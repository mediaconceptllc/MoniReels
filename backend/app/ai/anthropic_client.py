"""Thin Anthropic Messages API client using JSON-schema structured output.

Model name always comes from ANTHROPIC_MODEL (config) - never hardcoded here.
Mirrors app.ai.openai_client's shape (same complete_json signature, both
implement app.ai.llm_client.LLMClient) so app.ai.suggest can drive either
provider identically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from app.ai.llm_client import LLMError
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Same reasoning as OpenAIClient.MAX_COMPLETION_TOKENS: measured worst-case
# output for 3 shorts + 3 YouTube plans of strict JSON is ~3.3k tokens even
# in a token-dense language: this leaves generous headroom without paying
# for (or waiting on) a needlessly large completion.
MAX_TOKENS = 8000


class AnthropicError(LLMError):
    pass


@dataclass
class AnthropicConfig:
    api_key: str
    model: str


class AnthropicClient:
    def __init__(self, config: AnthropicConfig, client: anthropic.AsyncAnthropic | None = None) -> None:
        self._config = config
        self._client = client or anthropic.AsyncAnthropic(api_key=config.api_key, timeout=600.0)
        self._owns_client = client is None
        # Thinking is on by default on Opus 5 / Sonnet 5, and shares its
        # token budget with the actual response - left on, it risks the same
        # slow/truncated-output failure mode this app already hit once with
        # OpenAI's reasoning-tier gpt-5 (see openai_client.py). This task is
        # a structured cutting/JSON task, not one that benefits from deep
        # reasoning, so thinking is disabled by default. Haiku 4.5 has no
        # thinking unless explicitly requested, so it's simply omitted for
        # that tier. If a manually-entered model rejects a disabled
        # `thinking` param (e.g. Claude Fable 5, which forbids it outright),
        # detected once and not retried for the rest of this client's life.
        self._thinking_disable_unsupported = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def complete_json(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float = 0.4
    ) -> dict:
        if not self._config.api_key or not self._config.model:
            raise AnthropicError("ANTHROPIC_API_KEY / ANTHROPIC_MODEL are not configured")

        kwargs = self._build_kwargs(system, user, json_schema)
        try:
            response = await self._post(kwargs)
        except AnthropicError as e:
            thinking_sent = "thinking" in kwargs
            if self._thinking_disable_unsupported or not thinking_sent or not _is_unsupported_thinking(e):
                raise
            logger.warning(
                "Model %s does not support disabling thinking; retrying without it.", self._config.model
            )
            self._thinking_disable_unsupported = True
            kwargs.pop("thinking")
            response = await self._post(kwargs)

        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise AnthropicError(f"Anthropic declined the request (refusal, category={category})")

        try:
            text = next(block.text for block in response.content if block.type == "text")
        except StopIteration as e:
            raise AnthropicError(
                f"Unexpected Anthropic response shape: no text block in {response.content}"
            ) from e

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            truncated = response.stop_reason == "max_tokens"
            hint = " (response hit max_tokens - output may be truncated)" if truncated else ""
            raise AnthropicError(
                f"Anthropic response content was not valid JSON{hint}: {text[:500]}"
            ) from e

    def _build_kwargs(self, system: str, user: str, json_schema: dict) -> dict:
        schema = _strip_array_size_constraints(json_schema)
        kwargs: dict = {
            "model": self._config.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if not self._thinking_disable_unsupported and "haiku" not in self._config.model.lower():
            kwargs["thinking"] = {"type": "disabled"}
        return kwargs

    async def _post(self, kwargs: dict):
        try:
            return await self._client.messages.create(**kwargs)
        except anthropic.APITimeoutError as e:
            raise AnthropicError(
                f"Anthropic request timed out after {self._client.timeout:.0f}s ({type(e).__name__}) - "
                "the model may be slow on a long transcript; try again or use a smaller/faster model"
            ) from e
        except anthropic.APIStatusError as e:
            raise AnthropicError(f"Anthropic request failed ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AnthropicError(f"Anthropic request failed: {e or type(e).__name__}") from e


def _is_unsupported_thinking(e: AnthropicError) -> bool:
    msg = str(e).lower()
    return "thinking" in msg and ("disabl" in msg or "not support" in msg or "unsupported" in msg)


def _strip_array_size_constraints(schema: object) -> object:
    """Anthropic's structured-output JSON Schema support rejects minItems/
    maxItems values other than 0 or 1 (400 invalid_request_error: "For
    'array' type, 'minItems' values other than 0 or 1 are not supported").
    app.ai.prompts' schemas use these (e.g. cuts: 3-5, shorts: exactly 3) for
    OpenAI, which does support them - those schemas must stay unchanged, so
    the constraints are dropped here rather than at the source. Safe to
    drop: app.ai.schema's validate_shorts/postprocess_suggestions already
    enforce these exact counts in Python, independent of what the API
    itself validates.
    """
    if isinstance(schema, dict):
        return {
            key: _strip_array_size_constraints(value)
            for key, value in schema.items()
            if key not in ("minItems", "maxItems")
        }
    if isinstance(schema, list):
        return [_strip_array_size_constraints(item) for item in schema]
    return schema
