"""Provider-agnostic LLM types. Providers are thin; caching and budgets live in the client."""

from typing import Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """A provider call failed permanently (after retries)."""


class BudgetExceeded(LLMError):
    """The daily token or cost budget would be exceeded by this call."""


class LLMResult(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    cached: bool = False


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, *, system: str, prompt: str, max_output_tokens: int) -> LLMResult: ...


def estimate_tokens(text: str) -> int:
    """Rough pre-call estimate (~4 characters per token); good enough for a budget guard."""
    return max(1, len(text) // 4)
