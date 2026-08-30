"""Per-job LLM spend meter.

Every paid call goes through app.ai.openrouter_client.OpenRouterClient, so
counting there — at the single gate — covers every caller without touching
any handler. A context variable rather than a global: jobs run concurrently
in one worker process and their costs must not blend.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    models: list[str] = field(default_factory=list)

    def add(self, *, model: str, prompt: int, completion: int, cost: float) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cost_usd += cost
        if model and model not in self.models:
            self.models.append(model)

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            # Six places: individual calls on cheap models land well below a
            # cent and would round to 0.00, making a real bill look free.
            "cost_usd": round(self.cost_usd, 6),
            "models": self.models,
        }


_current: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)


def start() -> Usage:
    usage = Usage()
    _current.set(usage)
    return usage


def current() -> Usage | None:
    return _current.get()


def record(*, model: str, prompt: int, completion: int, cost: float) -> None:
    usage = _current.get()
    if usage is not None:
        usage.add(model=model, prompt=prompt, completion=completion, cost=cost)
