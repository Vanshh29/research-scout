"""HTTP fetching with exponential backoff, full jitter, and Retry-After support."""

import random
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from scout.log import get_logger

log = get_logger("http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(Exception):
    """Raised when a request fails permanently or exhausts its retries."""


def make_client(
    *, user_agent: str, timeout_s: float, headers: Mapping[str, str] | None = None
) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, **(headers or {})},
        timeout=timeout_s,
        follow_redirects=True,
    )


def backoff_delay(
    attempt: int, *, base: float = 1.0, cap: float = 30.0, rand: Callable[[], float] = random.random
) -> float:
    """Full-jitter backoff: uniform in [0, min(cap, base * 2**attempt)]."""
    return rand() * min(cap, base * 2.0**attempt)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    max_retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            if attempt == max_retries:
                raise FetchError(f"{url}: {exc}") from exc
            delay = backoff_delay(attempt)
            log.warning("network error, retrying", extra={"url": url, "attempt": attempt})
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRYABLE_STATUS or attempt == max_retries:
                raise FetchError(f"{url}: HTTP {resp.status_code}")
            delay = _retry_after(resp) or backoff_delay(attempt)
            log.warning(
                "retryable status",
                extra={"url": url, "status": resp.status_code, "attempt": attempt},
            )
        sleep(delay)
    raise FetchError(f"{url}: retries exhausted")  # pragma: no cover
