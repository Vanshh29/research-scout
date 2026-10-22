"""Gemini via the public REST API (httpx, so no extra SDK dependency)."""

import time
from collections.abc import Callable

import httpx

from scout.ingest.http import RETRYABLE_STATUS, backoff_delay
from scout.llm.base import LLMError, LLMResult, estimate_tokens
from scout.log import get_logger

log = get_logger("llm.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    name = "gemini"

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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens, "temperature": 0.2},
        }
        url = f"{BASE_URL}/models/{self.model}:generateContent"
        for attempt in range(self._max_retries + 1):
            try:
                # key goes in a header, never the URL, so it can't leak into logs
                resp = self._client.post(url, json=body, headers={"x-goog-api-key": self._api_key})
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise LLMError(f"gemini: {exc}") from exc
            else:
                if resp.status_code < 400:
                    return self._parse(resp.json(), system + prompt)
                if resp.status_code not in RETRYABLE_STATUS or attempt == self._max_retries:
                    raise LLMError(f"gemini: HTTP {resp.status_code}")
                log.warning("retryable status", extra={"status": resp.status_code})
            self._sleep(backoff_delay(attempt))
        raise LLMError("gemini: retries exhausted")  # pragma: no cover

    def _parse(self, data: dict[str, object], sent: str) -> LLMResult:
        try:
            candidates = data["candidates"]
            parts = candidates[0]["content"]["parts"]  # type: ignore[index]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("gemini: unexpected response shape") from exc
        usage = data.get("usageMetadata")
        if not isinstance(usage, dict):
            usage = {}
        return LLMResult(
            text=text,
            input_tokens=int(usage.get("promptTokenCount", estimate_tokens(sent))),
            output_tokens=int(usage.get("candidatesTokenCount", estimate_tokens(text))),
            model=self.model,
        )
