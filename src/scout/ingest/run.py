"""Run every source, persist results, and report per-source status without crashing."""

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

from scout.ingest.base import Source
from scout.log import get_logger
from scout.store.db import upsert_item

log = get_logger("ingest")


@dataclass
class SourceReport:
    source: str
    status: Literal["ok", "failed"]
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    latency_ms: int = 0
    error: str | None = None


def ingest_sources(
    conn: sqlite3.Connection, sources: Sequence[Source], client: httpx.Client
) -> list[SourceReport]:
    reports = []
    for source in sources:
        started = time.monotonic()
        try:
            items = source.fetch(client)
        except Exception as exc:  # one bad source must not stop the run
            report = SourceReport(source=source.name, status="failed", error=str(exc))
            log.exception("source failed", extra={"source": source.name})
        else:
            report = SourceReport(source=source.name, status="ok", fetched=len(items))
            for item in items:
                result = upsert_item(conn, item)
                setattr(report, result, getattr(report, result) + 1)
        report.latency_ms = int((time.monotonic() - started) * 1000)
        log.info("source done", extra=vars(report))
        reports.append(report)
    return reports
