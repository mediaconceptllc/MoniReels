"""The seam between app.ai.suggest and whichever model actually answers.

One method. That is the whole reason swapping two direct provider clients
(OpenAI and Anthropic) for OpenRouter touched exactly one file and left
prompts.py, schema.py and suggest.py alone.

Keep it this narrow. Anything provider-shaped that leaks through here —
a model name, a token budget, a vendor's error type — moves the next
migration back into the callers.
"""
from __future__ import annotations

from typing import Protocol


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    async def complete_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        temperature: float = 0.4,
        max_tokens: int | None = None,
    ) -> dict: ...

    async def aclose(self) -> None: ...
