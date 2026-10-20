"""Telegram Bot API delivery. The token is part of the URL, so errors never echo it."""

import time
from collections.abc import Callable

import httpx

from scout.ingest.http import RETRYABLE_STATUS, backoff_delay


class DeliveryError(Exception):
    """A message could not be delivered."""


def send_message(
    client: httpx.Client,
    *,
    token: str,
    chat_id: str,
    text: str,
    max_retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(max_retries + 1):
        try:
            resp = client.post(url, json=body)
        except httpx.TransportError as exc:
            if attempt == max_retries:
                raise DeliveryError(f"telegram: {type(exc).__name__}") from None
        else:
            if resp.status_code < 400:
                return
            if resp.status_code not in RETRYABLE_STATUS or attempt == max_retries:
                raise DeliveryError(f"telegram: HTTP {resp.status_code}")
        sleep(backoff_delay(attempt))
