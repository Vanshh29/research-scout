"""Read models for the dashboard: plain functions from a connection to small dataclasses.

Routes stay thin and these are testable without HTTP."""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from scout.brief import Brief, BriefEntry, build_brief
from scout.graph.queries import trending
from scout.store.runs import last_run, source_statuses

PREVIEW_TOP_N = 8
HISTORY_LIMIT = 14
CHANGED_ITEMS = 10
CHANGED_CONCEPTS = 12
CHANGED_TRENDS = 5


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


@dataclass(frozen=True)
class ChangedItem:
    item_id: str
    title: str
    url: str
    source: str
    tldr: str | None
    hybrid: float | None


@dataclass(frozen=True)
class SourceChange:
    source: str
    before: str | None  # None: the source was not in the previous run
    after: str


@dataclass(frozen=True)
class TrendRow:
    name: str
    recent: int
    is_new: bool


@dataclass(frozen=True)
class ChangedPage:
    since: str | None  # start of the latest run; None when no run exists
    total_new: int
    new_items: list[ChangedItem]
    new_concepts: list[str]
    trends: list[TrendRow]
    source_changes: list[SourceChange]


def _source_changes(conn: sqlite3.Connection) -> list[SourceChange]:
    """Sources whose status differs between the two newest runs."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 2")]
    if len(ids) < 2:
        return []
    before = {s["source"]: s["status"] for s in source_statuses(conn, ids[1])}
    return [
        SourceChange(s["source"], before.get(s["source"]), s["status"])
        for s in source_statuses(conn, ids[0])
        if before.get(s["source"]) != s["status"]
    ]


def changed_today(conn: sqlite3.Connection, *, now: datetime | None = None) -> ChangedPage:
    """What the latest run added: items first seen since it started (best scored first), concepts
    first seen since then, concepts gaining mentions, and sources whose health changed."""
    latest = last_run(conn)
    if latest is None:
        return ChangedPage(None, 0, [], [], [], [])
    since = latest["started_at"]
    total = conn.execute(
        "SELECT COUNT(*) FROM items WHERE first_seen_at >= ?", (since,)
    ).fetchone()[0]
    rows = conn.execute(
        """SELECT i.id, i.title, i.url, i.source, sc.hybrid,
                  (SELECT tldr FROM latest_summaries s WHERE s.item_id = i.id) AS tldr
           FROM items i LEFT JOIN scores sc ON sc.item_id = i.id
           WHERE i.first_seen_at >= ?
           ORDER BY (sc.hybrid IS NULL), sc.hybrid DESC, i.id LIMIT ?""",
        (since, CHANGED_ITEMS),
    ).fetchall()
    concepts = conn.execute(
        """SELECT name FROM entities
           WHERE item_id IS NULL AND type != 'Person' AND first_seen_at >= ?
           ORDER BY name LIMIT ?""",
        (since, CHANGED_CONCEPTS),
    ).fetchall()
    trends = trending(conn, limit=CHANGED_TRENDS, now=now)
    return ChangedPage(
        since=since,
        total_new=total,
        new_items=[
            ChangedItem(r["id"], r["title"], r["url"], r["source"], r["tldr"], r["hybrid"])
            for r in rows
        ],
        new_concepts=[c["name"] for c in concepts],
        trends=[TrendRow(t.node.name, t.recent, t.is_new) for t in trends],
        source_changes=_source_changes(conn),
    )
