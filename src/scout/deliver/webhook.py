"""Optional hook: POST the brief JSON to an n8n (or any) webhook. A no-op when unset (ADR-009)."""

import httpx

from scout.brief import Brief
from scout.deliver.telegram import DeliveryError
from scout.process.sanitize import is_http_url


def post_brief(client: httpx.Client, url: str | None, brief: Brief) -> bool:
    """Returns True if posted, False if no webhook is configured."""
    if not url:
        return False
    if not is_http_url(url):
        raise DeliveryError("webhook: URL must be http(s)")
    try:
        resp = client.post(url, json=brief.model_dump(mode="json"))
    except httpx.TransportError as exc:
        raise DeliveryError(f"webhook: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        raise DeliveryError(f"webhook: HTTP {resp.status_code}")
    return True
