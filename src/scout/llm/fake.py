"""Deterministic offline provider for tests, CI, and the public demo."""

import hashlib
from collections.abc import Callable

from scout.llm.base import LLMResult, estimate_tokens

Responder = Callable[[str, str], str]


def _default_responder(system: str, prompt: str) -> str:
    return "fake:" + hashlib.sha256(f"{system}\n{prompt}".encode()).hexdigest()[:12]


class FakeProvider:
    name = "fake"

    def __init__(self, *, model: str = "fake-1", responder: Responder | None = None) -> None:
        self.model = model
        self._responder = responder or _default_responder
        self.calls = 0

    def complete(self, *, system: str, prompt: str, max_output_tokens: int) -> LLMResult:
        self.calls += 1
        text = self._responder(system, prompt)
        return LLMResult(
            text=text,
            input_tokens=estimate_tokens(system + prompt),
            output_tokens=estimate_tokens(text),
            model=self.model,
        )
