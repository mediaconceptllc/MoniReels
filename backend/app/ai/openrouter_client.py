"""OpenRouter — the only LLM provider.

Replaces the desktop build's separate OpenAI and Anthropic clients. Both are
reachable through OpenRouter's OpenAI-compatible Chat Completions endpoint,
so the choice between them becomes a model string instead of a second client,
a second key, a second config block and a provider-switch in the UI.

Everything the two old clients learned the hard way is preserved here,
because none of it was provider-specific — it all still happens through
OpenRouter, just with the upstream error text relayed:

* An uncapped `max_tokens` makes the provider reserve the model's entire
  output ceiling against the account's per-minute limit, which is what
  actually triggers 429s on small accounts — not the transcript size.
* Some models reject a custom `temperature` outright.
* Some models reject JSON-Schema `minItems`/`maxItems` above 1.

Each of those is detected once from the real error and remembered for the
rest of the client's life, rather than hardcoded against a model-name list
that goes stale the moment a new model ships.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.ai import usage as usage_meter
from app.ai.llm_client import LLMError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Measured worst case for 3 shorts + 3 YouTube plans of strict JSON is ~3.3k
# tokens even in a token-dense language. 8000 leaves ~3x headroom while still
# fitting under a 30k-TPM tier alongside the largest prompt this app sends.
MAX_TOKENS = 8000

# A reasoning-tier model on a 45k-character transcript genuinely runs for
# minutes with no intermediate output.
REQUEST_TIMEOUT_SEC = 600.0


class OpenRouterError(LLMError):
    pass


@dataclass
class OpenRouterConfig:
    api_key: str
    model: str
    base_url: str = "https://openrouter.ai/api/v1"
    app_url: str = ""
    app_title: str = "MoniReels"


class OpenRouterClient:
    """Implements app.ai.llm_client.LLMClient, so app.ai.suggest and
    app.ai.prompts are untouched by the provider change."""

    def __init__(self, config: OpenRouterConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        self._owns_client = http_client is None
        self._temperature_unsupported = False
        self._array_bounds_unsupported = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete_json(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float = 0.4
    ) -> dict:
        if not self._config.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")
        if not self._config.model:
            raise OpenRouterError("OPENROUTER_MODEL is not configured")

        body = self._build_body(system, user, json_schema, schema_name, temperature)
        data = await self._post_with_capability_fallbacks(
            body, system, user, json_schema, schema_name, temperature
        )

        self._record_usage(data)

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError) as e:
            raise OpenRouterError(f"Unexpected OpenRouter response shape: {_truncate(data)}") from e

        if not content:
            reason = data["choices"][0].get("finish_reason")
            raise OpenRouterError(f"OpenRouter returned an empty completion (finish_reason={reason})")

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            truncated = data["choices"][0].get("finish_reason") == "length"
            hint = " (hit the token limit — output is truncated)" if truncated else ""
            raise OpenRouterError(f"Response was not valid JSON{hint}: {content[:500]}") from e

    # -- capability probing ------------------------------------------------

    async def _post_with_capability_fallbacks(
        self, body: dict, system: str, user: str, json_schema: dict, schema_name: str, temperature: float
    ) -> dict:
        """Send, and on a capability error strip the offending feature once
        and resend. Each fallback is remembered so the probe costs at most
        one extra round trip per client, not per call."""
        try:
            return await self._post(body)
        except OpenRouterError as e:
            if not self._temperature_unsupported and "temperature" in body and _is_temperature_error(e):
                logger.warning("Model %s rejects a custom temperature; dropping it.", self._config.model)
                self._temperature_unsupported = True
                body.pop("temperature", None)
                return await self._post_with_capability_fallbacks(
                    body, system, user, json_schema, schema_name, temperature
                )
            if not self._array_bounds_unsupported and _is_array_bounds_error(e):
                # Anthropic-backed models reject minItems/maxItems above 1.
                # Safe to drop: app.ai.schema enforces the exact counts
                # ("exactly 3 shorts", "3-5 cuts") in Python regardless of
                # what the provider itself validated.
                logger.warning(
                    "Model %s rejects array size constraints; stripping them.", self._config.model
                )
                self._array_bounds_unsupported = True
                body = self._build_body(system, user, json_schema, schema_name, temperature)
                return await self._post_with_capability_fallbacks(
                    body, system, user, json_schema, schema_name, temperature
                )
            raise

    def _build_body(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float
    ) -> dict:
        schema = _strip_array_size_constraints(json_schema) if self._array_bounds_unsupported else json_schema
        body: dict = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            },
            "max_tokens": MAX_TOKENS,
            # Ask OpenRouter for the real charge instead of keeping a price
            # table in code — a hardcoded table is stale the day a model's
            # price changes, and silently wrong rather than visibly missing.
            "usage": {"include": True},
        }
        if not self._temperature_unsupported:
            body["temperature"] = temperature
        return body

    async def _post(self, body: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        # Optional attribution headers. Neither affects billing or routing.
        if self._config.app_url:
            headers["HTTP-Referer"] = self._config.app_url
        if self._config.app_title:
            headers["X-Title"] = self._config.app_title

        try:
            response = await self._client.post(
                f"{self._config.base_url.rstrip('/')}/chat/completions", headers=headers, json=body
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise OpenRouterError(
                f"OpenRouter request failed ({e.response.status_code}): {e.response.text[:600]}"
            ) from e
        except httpx.TimeoutException as e:
            raise OpenRouterError(
                f"OpenRouter request timed out after {REQUEST_TIMEOUT_SEC:.0f}s ({type(e).__name__}) — "
                "the model may be slow on a long transcript; try a faster model"
            ) from e
        except httpx.HTTPError as e:
            raise OpenRouterError(f"OpenRouter request failed: {e or type(e).__name__}") from e

        data = response.json()
        # OpenRouter reports upstream provider failures as a 200 with an
        # `error` object rather than an HTTP error status.
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise OpenRouterError(f"OpenRouter upstream error: {message}")
        return data

    def _record_usage(self, data: dict) -> None:
        usage = data.get("usage") or {}
        usage_meter.record(
            model=data.get("model") or self._config.model,
            prompt=int(usage.get("prompt_tokens") or 0),
            completion=int(usage.get("completion_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
        )


def build_client(settings) -> OpenRouterClient:
    return OpenRouterClient(
        OpenRouterConfig(
            api_key=settings.openrouter_api_key,
            model=settings.openrouter_model,
            base_url=settings.openrouter_base_url,
            app_url=settings.openrouter_app_url,
            app_title=settings.openrouter_app_title,
        )
    )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _is_temperature_error(e: Exception) -> bool:
    msg = str(e).lower()
    return "temperature" in msg and any(
        marker in msg for marker in ("unsupported", "does not support", "not supported", "only the default")
    )


def _is_array_bounds_error(e: Exception) -> bool:
    msg = str(e).lower()
    return ("minitems" in msg or "maxitems" in msg) and (
        "not supported" in msg or "unsupported" in msg or "invalid" in msg
    )


def _strip_array_size_constraints(schema: object) -> object:
    if isinstance(schema, dict):
        return {
            key: _strip_array_size_constraints(value)
            for key, value in schema.items()
            if key not in ("minItems", "maxItems")
        }
    if isinstance(schema, list):
        return [_strip_array_size_constraints(item) for item in schema]
    return schema


def _truncate(data: object, limit: int = 400) -> str:
    return str(data)[:limit]
