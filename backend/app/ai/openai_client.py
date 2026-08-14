"""Thin OpenAI Chat Completions client using JSON-schema structured output.

Model name always comes from OPENAI_MODEL (config) — never hardcoded here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.ai.llm_client import LLMError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Without an explicit cap, OpenAI reserves the model's entire max-output
# ceiling (tens of thousands of tokens for gpt-4.1/gpt-5) against the
# account's tokens-per-minute rate limit, even though 3 shorts + 3 YouTube
# plans of strict JSON only ever produce a few thousand tokens (measured:
# ~3.3k tokens for a maximally-sized Mongolian response). That reservation,
# not the transcript itself, is what tips small-tier accounts into 429
# rate_limit_exceeded. Capping it here shrinks the counted request size
# without touching real output. 8000 leaves ~3x headroom over the measured
# worst case while still fitting under a 30k TPM account tier alongside the
# largest chunk this app currently sends (~20k prompt tokens).
MAX_COMPLETION_TOKENS = 8000


class OpenAIError(LLMError):
    pass


@dataclass
class OpenAIConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"


class OpenAIClient:
    def __init__(self, config: OpenAIConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        # Reasoning-tier models (o1/o3/gpt-5) can genuinely take several
        # minutes on a large transcript - prompts.py's CHAR_BUDGET (45k
        # chars, up from a former 12k) plus the model's own internal
        # reasoning steps regularly pushed past the old 120s timeout,
        # failing suggestion generation outright with an opaque
        # httpx.ReadTimeout partway through an otherwise-fine request.
        self._client = http_client or httpx.AsyncClient(timeout=600.0)
        self._owns_client = http_client is None
        # Some models (OpenAI's reasoning-tier models, e.g. o1/o3/gpt-5) only
        # accept the default temperature (1) and 400 on anything else. Rather
        # than hardcode a model-name list that goes stale the moment a new
        # model ships, detect the real error once and stop sending the field
        # for the rest of this client's lifetime.
        self._temperature_unsupported = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete_json(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float = 0.4
    ) -> dict:
        if not self._config.api_key or not self._config.model:
            raise OpenAIError("OPENAI_API_KEY / OPENAI_MODEL are not configured")

        body = self._build_body(system, user, json_schema, schema_name, temperature)
        try:
            data = await self._post(body)
        except OpenAIError as e:
            if self._temperature_unsupported or "temperature" not in body or not _is_unsupported_temperature(e):
                raise
            logger.warning(
                "Model %s does not support a custom temperature; retrying without it.", self._config.model
            )
            self._temperature_unsupported = True
            body.pop("temperature")
            data = await self._post(body)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise OpenAIError(f"Unexpected OpenAI response shape: {data}") from e

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise OpenAIError(f"OpenAI response content was not valid JSON: {content[:500]}") from e

    def _build_body(self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float) -> dict:
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            },
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        if not self._temperature_unsupported:
            body["temperature"] = temperature
        return body

    async def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        try:
            response = await self._client.post(
                f"{self._config.base_url}/chat/completions", headers=headers, json=body
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise OpenAIError(f"OpenAI request failed ({status}): {e.response.text[:500]}") from e
        except httpx.TimeoutException as e:
            timeout = self._client.timeout.read
            raise OpenAIError(
                f"OpenAI request timed out after {timeout:.0f}s ({type(e).__name__}) - "
                "the model may be slow on a long transcript; try again or use a smaller/faster model"
            ) from e
        except httpx.HTTPError as e:
            raise OpenAIError(f"OpenAI request failed: {e or type(e).__name__}") from e
        return response.json()


def _is_unsupported_temperature(e: OpenAIError) -> bool:
    msg = str(e).lower()
    return "temperature" in msg and ("unsupported" in msg or "does not support" in msg)
