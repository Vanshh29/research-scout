"""Run records: one row per pipeline run plus per-source health."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from scout.ingest.run import SourceReport


def start_run(
    conn: sqlite3.Connection, *, trigger: str = "manual", now: datetime | None = None
) -> int:
    started = (now or datetime.now(UTC)).isoformat()
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, status, trigger) VALUES (?, 'running', ?)",
            (started, trigger),
        )
    assert cur.lastrowid is not None
    return cur.lastrowid


def run_status(reports: Sequence[SourceReport]) -> str:
    """failed: every source failed; partial: some failed; empty: nothing fetched; else ok."""
    failed = [r for r in reports if r.status == "failed"]
    if reports and len(failed) == len(reports):
        return "failed"
    if failed:
        return "partial"
    if sum(r.fetched for r in reports) == 0:
        return "empty"
    return "ok"


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    reports: Sequence[SourceReport],
    *,
    latency_ms: int,
    tokens: int = 0,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> str:
    status = run_status(reports)
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO source_status
                   (run_id, source, status, error, items_fetched, items_new, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (run_id, r.source, r.status, r.error, r.fetched, r.new, r.latency_ms)
                for r in reports
            ],
        )
        conn.execute(
            """UPDATE runs SET finished_at = ?, status = ?, items_fetched = ?, items_new = ?,
                   items_failed = ?, latency_ms = ?, tokens = ?, cost_usd = ? WHERE id = ?""",
            (
                (now or datetime.now(UTC)).isoformat(),
                status,
                sum(r.fetched for r in reports),
                sum(r.new for r in reports),
                sum(1 for r in reports if r.status == "failed"),
                latency_ms,
                tokens,
                cost_usd,
                run_id,
            ),
        )
    return status


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row


def source_statuses(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_status WHERE run_id = ? ORDER BY source", (run_id,)
    ).fetchall()
