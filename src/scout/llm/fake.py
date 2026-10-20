"""Deterministic offline provider for tests, CI, and the public demo."""

import hashlib
import json
import re
from collections.abc import Callable

from scout.llm.base import LLMResult, estimate_tokens

Responder = Callable[[str, str], str]


def _summary_json(prompt: str) -> str:
    """Valid summary JSON built from the document text, so offline runs look realistic."""
    m = re.search(r"<document[^>]*>\n(.*?)\n</document>", prompt, re.DOTALL)
    text = (m.group(1) if m else "").strip()
    title = text.split("\n", 1)[0][:120]
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", text)]
    return json.dumps(
        {
            "tldr": f"Fake summary of: {title}",
            "key_points": [f"mentions {w}" for w in words[:3]],
            "entities": sorted(set(words[:5])),
        }
    )


def _score_json(prompt: str) -> str:
    """Valid rubric JSON: deterministic per document; more topic words score higher."""
    h = int(hashlib.sha256(prompt.encode()).hexdigest(), 16)
    hits = len(re.findall(r"\b(?:agent|agents|rag|retrieval|eval|benchmark|judge)\b", prompt, re.I))
    base = min(0.9, 0.2 + 0.1 * hits)
    return json.dumps(
        {
            "relevance": base,
            "novelty": round(0.3 + (h % 40) / 100, 2),
            "evidence_quality": round(0.3 + ((h >> 8) % 40) / 100, 2),
            "actionability": round(0.3 + ((h >> 16) % 40) / 100, 2),
            "reason": "Fake rubric score (offline provider).",
        }
    )


def _default_responder(system: str, prompt: str) -> str:
    if "[task:summarize]" in system:
        return _summary_json(prompt)
    if "[task:score]" in system:
        return _score_json(prompt)
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
