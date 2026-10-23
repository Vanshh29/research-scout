"""Write guard. Default-deny: demo mode and a missing token both refuse every write.

A browser unlocks once with the token (`POST /unlock`) and gets a cookie holding a keyed hash of it,
never the token itself. API clients send `Authorization: Bearer <token>`. Cookie requests must also
carry the `X-Scout` header, which a cross-site form cannot set (CSRF)."""

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from scout.config import Settings

COOKIE = "scout_unlock"
CSRF_HEADER = "x-scout"


def writes_enabled(settings: Settings) -> bool:
    return not settings.demo and bool(settings.api_token)


def cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"scout-unlock-v1", hashlib.sha256).hexdigest()


def token_matches(settings: Settings, candidate: str) -> bool:
    return bool(settings.api_token) and secrets.compare_digest(
        candidate.encode(), (settings.api_token or "").encode()
    )


def is_unlocked(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    if not writes_enabled(settings):
        return False
    cookie = request.cookies.get(COOKIE, "")
    return secrets.compare_digest(cookie.encode(), cookie_value(settings.api_token or "").encode())


def refuse_if_disabled(settings: Settings) -> None:
    if settings.demo:
        raise HTTPException(403, "Read-only demo: writes are refused.")
    if not settings.api_token:
        raise HTTPException(403, "Writes are disabled: SCOUT_API_TOKEN is not set.")


def require_write(request: Request) -> None:
    settings: Settings = request.app.state.settings
    refuse_if_disabled(settings)
    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token_matches(settings, bearer.strip()):
        return
    if is_unlocked(request):
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "Missing X-Scout header.")
        return
    raise HTTPException(401, "Invalid or missing token.")
