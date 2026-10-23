"""The dashboard: FastAPI + Jinja2, read-only by default. Every fetched string is untrusted, so
templates autoescape, links pass `is_http_url`, and a strict CSP forbids inline script."""

import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from scout.api import views
from scout.api.auth import (
    COOKIE,
    cookie_value,
    is_unlocked,
    refuse_if_disabled,
    require_write,
    token_matches,
    writes_enabled,
)
from scout.config import Settings
from scout.graph.queries import subgraph
from scout.process.sanitize import is_http_url
from scout.store.db import connect, migrate
from scout.store.feedback import UnknownItemError, get_votes, set_vote

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
NAV = [("Today", "/"), ("Changed", "/changed"), ("Graph", "/graph"), ("Runs", "/runs")]
RUBRIC = [
    ("relevance", "Relevance"),
    ("novelty", "Novelty"),
    ("evidence_quality", "Evidence"),
    ("actionability", "Actionability"),
]
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def when(stamp: str | None) -> str:
    """'22 Oct, 14:05 UTC'."""
    parsed = _parse(stamp)
    return "" if parsed is None else parsed.astimezone(UTC).strftime("%d %b, %H:%M UTC").lstrip("0")


def ago(stamp: str | None, now: datetime | None = None) -> str:
    parsed = _parse(stamp)
    if parsed is None:
        return "never"
    seconds = int(((now or datetime.now(UTC)) - parsed).total_seconds())
    if seconds < 90:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def duration(ms: int) -> str:
    return f"{ms / 1000:.1f} s" if ms < 60_000 else f"{ms / 60_000:.1f} min"


def safe_url(url: str) -> str:
    """The URL if it is plain http(s), else an empty string so the template renders no link."""
    return url if is_http_url(url) else ""


class FeedbackIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    vote: Literal[-1, 1]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    boot = connect(settings.db_path)
    try:
        migrate(boot)  # a snapshot from an older version is upgraded on start
    finally:
        boot.close()

    app = FastAPI(title="Research Scout", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters.update(when=when, ago=ago, duration=duration, safe_url=safe_url)
    templates.env.globals.update(rubric=RUBRIC)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def render(
        request: Request,
        conn: sqlite3.Connection,
        name: str,
        active: str,
        item_ids: Sequence[str] = (),
        **ctx: Any,
    ) -> HTMLResponse:
        latest = views.runs_page(conn).last
        return templates.TemplateResponse(
            request,
            name,
            {
                "nav": NAV,
                "active": active,
                "demo": settings.demo,
                "writes": writes_enabled(settings),
                "unlocked": is_unlocked(request),
                "votes_by_item": get_votes(conn, item_ids),
                "last_run": latest,
                **ctx,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, conn: Conn) -> HTMLResponse:
        brief = views.todays_brief(conn)
        ids = [e.item_id for e in brief.entries]
        return render(request, conn, "brief.html", "/", item_ids=ids, brief=brief)

    @app.get("/changed", response_class=HTMLResponse)
    def changed(request: Request, conn: Conn) -> HTMLResponse:
        page = views.changed_today(conn)
        ids = [i.item_id for i in page.new_items]
        return render(request, conn, "changed.html", "/changed", item_ids=ids, page=page)

    Center = Annotated[str | None, Query(max_length=100)]

    @app.get("/graph", response_class=HTMLResponse)
    def graph(request: Request, conn: Conn, center: Center = None) -> HTMLResponse:
        return render(request, conn, "graph.html", "/graph", center=center or "")

    @app.get("/api/graph")
    def graph_data(
        conn: Conn, center: Center = None, depth: Annotated[int, Query(ge=1, le=2)] = 1
    ) -> JSONResponse:
        sub = subgraph(conn, center.strip() or None if center else None, depth=depth)
        return JSONResponse(
            {
                "center": sub.center,
                "nodes": [vars(n) for n in sub.nodes],
                "edges": [vars(e) for e in sub.edges],
                "items": [
                    {"id": i.id, "title": i.title, "url": safe_url(i.url)} for i in sub.items
                ],
                "truncated": sub.truncated,
            }
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request, conn: Conn) -> HTMLResponse:
        return render(request, conn, "runs.html", "/runs", page=views.runs_page(conn))

    @app.post("/unlock")
    async def unlock(request: Request) -> Response:
        refuse_if_disabled(settings)
        raw = (await request.body())[:2048].decode("utf-8", errors="replace")
        token = parse_qs(raw).get("token", [""])[0]
        if not token_matches(settings, token):
            raise HTTPException(401, "Invalid token.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            cookie_value(token),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    @app.post("/api/feedback", dependencies=[Depends(require_write)])
    def feedback(conn: Conn, body: FeedbackIn) -> JSONResponse:
        try:
            vote = set_vote(conn, body.item_id, body.vote)
        except UnknownItemError:
            raise HTTPException(404, "Unknown item.") from None
        return JSONResponse({"item_id": body.item_id, "vote": vote})

    @app.get("/healthz")
    def healthz(conn: Conn) -> JSONResponse:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        return JSONResponse({"status": "ok", "schema": version, "demo": settings.demo})

    return app
