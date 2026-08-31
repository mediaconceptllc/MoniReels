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
from difflib import get_close_matches

import httpx

from app.ai import usage as usage_meter
from app.ai.llm_client import LLMError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Measured worst case for 3 shorts + 3 YouTube plans of strict JSON is ~3.3k
# tokens even in a token-dense language. 8000 leaves ~3x headroom while still
# fitting under a 30k-TPM tier alongside the largest prompt this app sends.
#
# It is the DEFAULT, not the law: a caller whose answer is proportional to
# the transcript (app.ai.punctuate returns the text again) knows its own size
# and passes it. A single constant for every call is what let a request be
# sent that could not fit its own answer.
MAX_TOKENS = 8000

# MEASURED, both calls of one production job: HTTP 200, finish_reason=length,
# message content empty. The first had been generating for 4m40s. Two causes
# produce exactly that and cannot be told apart from outside — an answer too
# long to fit, or a budget that `max_tokens` shares with reasoning and that ran
# out before any content was written. `_spend` reports which, when the provider
# says; either way an identical retry buys the same silence, so the retry
# raises the ceiling instead — once, bounded, and only when nothing came back.
LENGTH_RETRY_MULTIPLIER = 2

# A reasoning-tier model on a 45k-character transcript genuinely runs for
# minutes with no intermediate output.
REQUEST_TIMEOUT_SEC = 600.0

# The catalogue is a small static list behind a settings form somebody is
# waiting on, not a generation. It gets a form's patience, not a model's.
CATALOG_TIMEOUT_SEC = 20.0


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
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> dict:
        if not self._config.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")
        if not self._config.model:
            raise OpenRouterError("OPENROUTER_MODEL is not configured")

        cap = max_tokens or MAX_TOKENS
        content, data = await self._attempt(system, user, json_schema, schema_name, temperature, cap)

        # Nothing at all, because the budget ran out first. A second identical
        # request buys the same silence; a bigger ceiling is the one thing that
        # can change the answer, so it is spent here rather than failing the job.
        if not content and _finish_reason(data) == "length":
            wider = cap * LENGTH_RETRY_MULTIPLIER
            logger.warning(
                "%s returned nothing within %d tokens (%s); retrying once at %d.",
                self._config.model, cap, _spend(data), wider,
            )
            content, data = await self._attempt(
                system, user, json_schema, schema_name, temperature, wider
            )
            cap = wider

        if not content:
            raise OpenRouterError(
                f"{self._config.model} returned an empty completion "
                f"(finish_reason={_finish_reason(data)}, budget={cap} tokens, {_spend(data)})"
            )

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            truncated = _finish_reason(data) == "length"
            hint = f" (hit the {cap}-token limit — output is truncated)" if truncated else ""
            raise OpenRouterError(f"Response was not valid JSON{hint}: {content[:500]}") from e

    async def _attempt(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict]:
        """One billed round trip. Returns the message content and the raw
        response, so the caller can tell "said nothing" from "said nothing
        because it ran out of room"."""
        body = self._build_body(system, user, json_schema, schema_name, temperature, max_tokens)
        data = await self._post_with_capability_fallbacks(
            body, system, user, json_schema, schema_name, temperature, max_tokens
        )
        self._record_usage(data)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise OpenRouterError(f"Unexpected OpenRouter response shape: {_truncate(data)}") from e
        return content or "", data

    # -- capability probing ------------------------------------------------

    async def _post_with_capability_fallbacks(
        self,
        body: dict,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        temperature: float,
        max_tokens: int,
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
                    body, system, user, json_schema, schema_name, temperature, max_tokens
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
                body = self._build_body(system, user, json_schema, schema_name, temperature, max_tokens)
                return await self._post_with_capability_fallbacks(
                    body, system, user, json_schema, schema_name, temperature, max_tokens
                )
            raise

    def _build_body(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        temperature: float,
        max_tokens: int = MAX_TOKENS,
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
            "max_tokens": max_tokens,
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


# ---------------------------------------------------------------------------
# The model catalogue
# ---------------------------------------------------------------------------
#
# A model name is one free-text string that every paid job depends on, and
# nothing checked it. `deepseek/deepseek-v4-flash-latest` was stored, looked
# saved, and turned three suggest jobs into 400s an hour later — the exact
# failure app.schemas already refuses for `stt_provider`, arriving through the
# field beside it.


class ModelCatalogUnavailable(OpenRouterError):
    """The catalogue could not be read.

    Deliberately NOT the same as "that model does not exist". An outage must
    not stop an operator changing settings, and an unreadable list must never
    be reported as a missing model — the fix for one is waiting, and for the
    other retyping.
    """


@dataclass(frozen=True)
class ModelCheck:
    """Three states, not two: known, unknown-with-suggestions, and
    unknown-with-none. Collapsing the last two throws away the only thing
    that turns "wrong" into "did you mean"."""

    known: bool
    suggestions: tuple[str, ...] = ()


async def check_model(
    config: OpenRouterConfig, model: str, http_client: httpx.AsyncClient | None = None
) -> ModelCheck:
    """Whether OpenRouter serves this model, and what it looks like if not.

    Raises ModelCatalogUnavailable when the answer is unknown rather than no.
    """
    ids = await fetch_model_ids(config, http_client)
    if model in ids:
        return ModelCheck(known=True)
    return ModelCheck(known=False, suggestions=tuple(get_close_matches(model, sorted(ids), n=5, cutoff=0.5)))


async def fetch_model_ids(
    config: OpenRouterConfig, http_client: httpx.AsyncClient | None = None
) -> set[str]:
    """Every model id OpenRouter will accept.

    CONTRACT NOT VERIFIED against the live docs — the network this was written
    on blocks openrouter.ai — so the parse is deliberately loose: the
    OpenAI-compatible `{"data": [{"id": ...}]}` shape, a bare list, and an
    id under `slug` are all read, and anything else raises
    ModelCatalogUnavailable rather than silently yielding an empty set. An
    empty set would mark EVERY model unknown and lock the settings page.

    The key is sent if there is one and the call is not required to need it:
    a 401 means the catalogue is unavailable, not that the model is wrong.
    """
    client = http_client or httpx.AsyncClient(timeout=CATALOG_TIMEOUT_SEC)
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        response = await client.get(f"{config.base_url.rstrip('/')}/models", headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as e:
        raise ModelCatalogUnavailable(f"Could not read the model list: {e or type(e).__name__}") from e
    except ValueError as e:  # not JSON
        raise ModelCatalogUnavailable("The model list was not JSON") from e
    finally:
        if http_client is None:
            await client.aclose()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ModelCatalogUnavailable(f"Unexpected model list shape: {_truncate(payload, 200)}")

    ids = {
        str(row.get("id") or row.get("slug"))
        for row in rows
        if isinstance(row, dict) and (row.get("id") or row.get("slug"))
    }
    if not ids:
        raise ModelCatalogUnavailable("The model list came back with no models in it")
    return ids


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


def _finish_reason(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def _spend(data: dict) -> str:
    """What the budget actually went on.

    "Empty completion" on its own does not say whether the model wrote a long
    answer that was cut off or thought until there was no room left to write
    one — and those have different fixes. Reasoning tokens are reported by
    providers that have them and simply absent elsewhere, so this reads them
    if they are there and stays quiet if they are not.
    """
    usage = data.get("usage") or {}
    completion = usage.get("completion_tokens")
    if completion is None:
        return "no usage reported"
    details = usage.get("completion_tokens_details") or {}
    reasoning = details.get("reasoning_tokens")
    if reasoning:
        return f"{completion} completion tokens, {reasoning} of them reasoning"
    return f"{completion} completion tokens"


def _truncate(data: object, limit: int = 400) -> str:
    return str(data)[:limit]
