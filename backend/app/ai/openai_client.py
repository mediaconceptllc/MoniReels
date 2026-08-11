"""Thin OpenAI Chat Completions client using JSON-schema structured output.

Model name always comes from OPENAI_MODEL (config) — never hardcoded here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from app.utils.logging import get_logger

logger = get_logger(__name__)


class OpenAIError(Exception):
    pass


@dataclass
class OpenAIConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"


class OpenAIClient:
    def __init__(self, config: OpenAIConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=120.0)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete_json(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float = 0.4
    ) -> dict:
        if not self._config.api_key or not self._config.model:
            raise OpenAIError("OPENAI_API_KEY / OPENAI_MODEL are not configured")

        body = {
            "model": self._config.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            },
        }
        headers = {"Authorization": f"Bearer {self._config.api_key}"}

        try:
            response = await self._client.post(
                f"{self._config.base_url}/chat/completions", headers=headers, json=body
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            raise OpenAIError(f"OpenAI request failed ({status}): {e.response.text[:500]}") from e
        except httpx.HTTPError as e:
            raise OpenAIError(f"OpenAI request failed: {e}") from e

        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise OpenAIError(f"Unexpected OpenAI response shape: {data}") from e

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise OpenAIError(f"OpenAI response content was not valid JSON: {content[:500]}") from e
