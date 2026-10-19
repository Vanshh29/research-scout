"""Chain the pipeline stages into one recorded run. A failing stage never aborts the run."""

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from scout.ingest.base import Source
from scout.ingest.run import SourceReport, ingest_sources
from scout.log import get_logger
from scout.store.runs import finish_run, start_run

log = get_logger("run_daily")


@dataclass
class RunSummary:
    run_id: int
    status: str
    items_fetched: int
    items_new: int
    reports: list[SourceReport]
    latency_ms: int


def run_daily(
    conn: sqlite3.Connection,
    sources: Sequence[Source],
    client: httpx.Client,
    *,
    trigger: str = "manual",
) -> RunSummary:
    """Today the chain is just ingest; summarize/rank/brief stages join it from Day 3."""
    run_id = start_run(conn, trigger=trigger)
    started = time.monotonic()
    reports: list[SourceReport] = []
    try:
        reports = ingest_sources(conn, sources, client)
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        status = finish_run(conn, run_id, reports, latency_ms=latency_ms)
    log.info("run finished", extra={"run_id": run_id, "status": status, "latency_ms": latency_ms})
    return RunSummary(
        run_id=run_id,
        status=status,
        items_fetched=sum(r.fetched for r in reports),
        items_new=sum(r.new for r in reports),
        reports=reports,
        latency_ms=latency_ms,
    )
