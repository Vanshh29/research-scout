"""Read models for the dashboard: plain functions from a connection to small dataclasses.

Routes stay thin and these are testable without HTTP."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from scout.brief import Brief, BriefEntry, build_brief
from scout.store.runs import last_run, source_statuses

PREVIEW_TOP_N = 8
HISTORY_LIMIT = 14


@dataclass(frozen=True)
class BriefPage:
    date: str
    created_at: str | None
    new_items: int
    entries: list[BriefEntry]
    trending: list[str]
    warnings: list[str]
    tokens: int
    cost_usd: float
    is_preview: bool  # no brief has been recorded yet; this is a live preview


@dataclass(frozen=True)
class SourceRow:
    source: str
    status: str
    fetched: int
    new: int
    latency_ms: int
    error: str | None


@dataclass(frozen=True)
class RunRow:
    id: int
    status: str
    trigger: str
    started_at: str
    finished_at: str | None
    fetched: int
    new: int
    failed_sources: int
    tokens: int
    cost_usd: float
    latency_ms: int


@dataclass(frozen=True)
class RunsPage:
    last: RunRow | None
    sources: list[SourceRow] = field(default_factory=list)
    history: list[RunRow] = field(default_factory=list)


def _page(brief: Brief, created_at: str | None, *, preview: bool) -> BriefPage:
    return BriefPage(
        date=brief.date,
        created_at=created_at,
        new_items=brief.new_items,
        entries=brief.entries,
        trending=brief.trending,
        warnings=brief.warnings,
        tokens=brief.tokens,
        cost_usd=brief.cost_usd,
        is_preview=preview,
    )


def todays_brief(conn: sqlite3.Connection, *, now: datetime | None = None) -> BriefPage:
    """The newest recorded brief that has items (a quiet day's empty brief does not hide the last
    useful one). With nothing recorded, a live preview of what the next brief would contain."""
    rows = conn.execute(
        "SELECT payload, created_at FROM briefs ORDER BY id DESC LIMIT 7"
    ).fetchall()
    briefs = [(Brief.model_validate_json(r["payload"]), r["created_at"]) for r in rows]
    for brief, created_at in briefs:
        if brief.entries:
            return _page(brief, created_at, preview=False)
    if briefs:
        return _page(briefs[0][0], briefs[0][1], preview=False)
    preview = build_brief(conn, top_n=PREVIEW_TOP_N, now=now or datetime.now(UTC))
    return _page(preview, None, preview=True)


def _run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        id=row["id"],
        status=row["status"],
        trigger=row["trigger"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        fetched=row["items_fetched"],
        new=row["items_new"],
        failed_sources=row["items_failed"],
        tokens=row["tokens"],
        cost_usd=row["cost_usd"],
        latency_ms=row["latency_ms"],
    )


def runs_page(conn: sqlite3.Connection) -> RunsPage:
    latest = last_run(conn)
    if latest is None:
        return RunsPage(last=None)
    history = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT,)
    ).fetchall()
    sources = [
        SourceRow(
            source=s["source"],
            status=s["status"],
            fetched=s["items_fetched"],
            new=s["items_new"],
            latency_ms=s["latency_ms"],
            error=s["error"],
        )
        for s in source_statuses(conn, latest["id"])
    ]
    return RunsPage(last=_run(latest), sources=sources, history=[_run(r) for r in history])
