"""Provider-agnostic interface shared by app.ai.openai_client and
app.ai.anthropic_client, so app.ai.suggest can drive whichever provider is
configured (Settings.ai_provider) without knowing which one it is.
"""
from __future__ import annotations

from typing import Protocol


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    async def complete_json(
        self, system: str, user: str, json_schema: dict, schema_name: str, temperature: float = 0.4
    ) -> dict: ...

    async def aclose(self) -> None: ...
