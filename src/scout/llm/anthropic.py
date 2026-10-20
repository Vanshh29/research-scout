"""Anthropic Messages API via httpx (optional provider)."""

import time
from collections.abc import Callable

import httpx

from scout.ingest.http import RETRYABLE_STATUS, backoff_delay
from scout.llm.base import LLMError, LLMResult, estimate_tokens

URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=60.0)
        self._max_retries = max_retries
        self._sleep = sleep

    def complete(self, *, system: str, prompt: str, max_output_tokens: int) -> LLMResult:
        body = {
            "model": self.model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(URL, json=body, headers=headers)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise LLMError(f"anthropic: {exc}") from exc
            else:
                if resp.status_code < 400:
                    return self._parse(resp.json(), system + prompt)
                if resp.status_code not in RETRYABLE_STATUS or attempt == self._max_retries:
                    raise LLMError(f"anthropic: HTTP {resp.status_code}")
            self._sleep(backoff_delay(attempt))
        raise LLMError("anthropic: retries exhausted")  # pragma: no cover

    def _parse(self, data: dict[str, object], sent: str) -> LLMResult:
        try:
            blocks = data["content"]
            text = "".join(b["text"] for b in blocks if b["type"] == "text")  # type: ignore[attr-defined]
        except (KeyError, TypeError) as exc:
            raise LLMError("anthropic: unexpected response shape") from exc
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return LLMResult(
            text=text,
            input_tokens=int(usage.get("input_tokens", estimate_tokens(sent))),
            output_tokens=int(usage.get("output_tokens", estimate_tokens(text))),
            model=self.model,
        )
