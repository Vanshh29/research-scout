"""Minimum-interval rate limiter (requests per minute), with an injectable clock."""

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_ok = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next_ok:
            self._sleep(self._next_ok - now)
            now = self._next_ok
        self._next_ok = now + self._interval
