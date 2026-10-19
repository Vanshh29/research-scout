import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scout.ingest.run import SourceReport
from scout.models import Item
from scout.scheduler.run_daily import run_daily
from scout.store.db import connect, migrate
from scout.store.runs import finish_run, last_run, source_statuses, start_run

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_migration_creates_run_tables(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "source_status"} <= tables


def test_run_lifecycle_records_totals_and_statuses(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn, trigger="cron", now=NOW)
    assert last_run(conn)["status"] == "running"  # type: ignore[index]
    reports = [
        SourceReport("arxiv", "ok", fetched=10, new=4, latency_ms=100),
        SourceReport("github", "failed", error="HTTP 403", latency_ms=50),
    ]
    finish_run(conn, run_id, reports, now=NOW, latency_ms=150)
    run = last_run(conn)
    assert run is not None
    assert (run["status"], run["items_fetched"], run["items_new"], run["items_failed"]) == (
        "partial",
        10,
        4,
        1,
    )
    statuses = {s["source"]: s for s in source_statuses(conn, run_id)}
    assert statuses["github"]["error"] == "HTTP 403"
    assert statuses["arxiv"]["items_new"] == 4


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        ([SourceReport("a", "ok", fetched=3, new=1)], "ok"),
        ([SourceReport("a", "failed"), SourceReport("b", "failed")], "failed"),
        ([SourceReport("a", "ok", fetched=0)], "empty"),
        ([], "empty"),
    ],
)
def test_run_status_rules(
    conn: sqlite3.Connection, reports: list[SourceReport], expected: str
) -> None:
    run_id = start_run(conn, now=NOW)
    finish_run(conn, run_id, reports, now=NOW, latency_ms=1)
    assert last_run(conn)["status"] == expected  # type: ignore[index]


class FakeSource:
    def __init__(self, name: str, items: list[Item] | Exception) -> None:
        self.name = name
        self._items = items

    def fetch(self, client: httpx.Client) -> list[Item]:
        if isinstance(self._items, Exception):
            raise self._items
        return self._items


def _item(n: int) -> Item:
    return Item.create(
        id=f"arxiv:{n}",
        source="arxiv",
        url=f"https://arxiv.org/abs/{n}",
        title=f"Paper {n}",
        authors=[],
        published_at=None,
        fetched_at=NOW,
        body="agents",
    )


def test_run_daily_records_run_and_survives_a_failed_source(conn: sqlite3.Connection) -> None:
    sources = [FakeSource("arxiv", [_item(1), _item(2)]), FakeSource("rss", RuntimeError("down"))]
    summary = run_daily(conn, sources, httpx.Client(), trigger="cron")
    assert summary.status == "partial"
    assert summary.items_new == 2
    run = last_run(conn)
    assert run is not None and run["finished_at"] is not None and run["trigger"] == "cron"
    assert {s["source"] for s in source_statuses(conn, summary.run_id)} == {"arxiv", "rss"}


def test_run_daily_marks_run_finished_even_if_everything_fails(conn: sqlite3.Connection) -> None:
    summary = run_daily(conn, [FakeSource("arxiv", RuntimeError("x"))], httpx.Client())
    assert summary.status == "failed"
    assert last_run(conn)["finished_at"] is not None  # type: ignore[index]
