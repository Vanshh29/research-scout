"""Chain the pipeline stages into one recorded run. A failing stage never aborts the run."""

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from scout.brief import Brief, build_brief, record_brief, render_telegram
from scout.config import Profile
from scout.deliver.telegram import DeliveryError, send_message
from scout.deliver.webhook import post_brief
from scout.ingest.base import Source
from scout.ingest.run import SourceReport, ingest_sources
from scout.llm.client import LLMClient
from scout.log import get_logger
from scout.process.summarize import summarize_pending
from scout.rank.embedder import Embedder
from scout.rank.hybrid import rank_items
from scout.store.runs import finish_run, start_run

log = get_logger("run_daily")


@dataclass
class Pipeline:
    """Everything the post-ingest stages need. Without one, run_daily only ingests."""

    embedder: Embedder
    profile: Profile
    prompts_dir: Path
    llm: LLMClient | None = None  # None: no key or provider failure; ranking is embedding-only
    llm_note: str | None = None  # why `llm` is None, shown in the brief
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None
    summarize_limit: int | None = 60
    since_days: int = 7


@dataclass
class RunSummary:
    run_id: int
    status: str
    items_fetched: int
    items_new: int
    reports: list[SourceReport]
    latency_ms: int
    brief: Brief | None = None
    warnings: list[str] = field(default_factory=list)


def _stage(name: str, warnings: list[str], fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:  # a broken stage must not stop the brief from shipping
        log.exception("stage failed", extra={"stage": name})
        warnings.append(f"{name} stage failed ({type(exc).__name__})")


def _deliver(
    conn: sqlite3.Connection,
    client: httpx.Client,
    pipeline: Pipeline,
    brief: Brief,
    run_id: int,
    warnings: list[str],
) -> None:
    """Send the brief. Items are marked shown only if delivery worked (or nothing is configured),
    so a failed send is retried with the same items on the next run."""
    delivered = True
    if pipeline.telegram_token and pipeline.telegram_chat_id:
        try:
            send_message(
                client,
                token=pipeline.telegram_token,
                chat_id=pipeline.telegram_chat_id,
                text=render_telegram(brief),
            )
        except DeliveryError as exc:
            delivered = False
            warnings.append(str(exc))
            log.warning("telegram delivery failed", extra={"error": str(exc)})
    try:
        post_brief(client, pipeline.webhook_url, brief)
    except DeliveryError as exc:
        warnings.append(str(exc))
        log.warning("webhook failed", extra={"error": str(exc)})
    if delivered:
        record_brief(conn, brief, run_id=run_id)


def _alert_failure(client: httpx.Client, pipeline: Pipeline, run_id: int, detail: str) -> None:
    if not (pipeline.telegram_token and pipeline.telegram_chat_id):
        return
    try:
        send_message(
            client,
            token=pipeline.telegram_token,
            chat_id=pipeline.telegram_chat_id,
            text=f"Research Scout run #{run_id} failed. {detail}",
        )
    except DeliveryError:
        log.warning("failure alert could not be delivered")


def run_daily(
    conn: sqlite3.Connection,
    sources: Sequence[Source],
    client: httpx.Client,
    *,
    trigger: str = "manual",
    pipeline: Pipeline | None = None,
) -> RunSummary:
    """ingest -> summarize -> rank -> brief -> deliver, recorded as one run."""
    run_id = start_run(conn, trigger=trigger)
    started = time.monotonic()
    reports: list[SourceReport] = []
    warnings: list[str] = []
    brief: Brief | None = None
    try:
        reports = ingest_sources(conn, sources, client)
        warnings += [
            f"source {r.source} failed: {r.error}" for r in reports if r.status == "failed"
        ]
        if pipeline is not None:
            brief = _post_ingest(conn, client, pipeline, run_id, warnings)
    finally:
        latency_ms = int((time.monotonic() - started) * 1000)
        llm = pipeline.llm if pipeline else None
        status = finish_run(
            conn,
            run_id,
            reports,
            latency_ms=latency_ms,
            tokens=llm.tokens_used if llm else 0,
            cost_usd=llm.cost_usd if llm else 0.0,
        )
    if status == "failed" and pipeline is not None:
        _alert_failure(client, pipeline, run_id, "; ".join(warnings)[:500])
    log.info("run finished", extra={"run_id": run_id, "status": status, "latency_ms": latency_ms})
    return RunSummary(
        run_id=run_id,
        status=status,
        items_fetched=sum(r.fetched for r in reports),
        items_new=sum(r.new for r in reports),
        reports=reports,
        latency_ms=latency_ms,
        brief=brief,
        warnings=warnings,
    )


def _post_ingest(
    conn: sqlite3.Connection,
    client: httpx.Client,
    pipeline: Pipeline,
    run_id: int,
    warnings: list[str],
) -> Brief:
    llm = pipeline.llm
    if llm is None and pipeline.llm_note:
        warnings.append(pipeline.llm_note)

    def summarize() -> None:
        if llm is None:
            return
        rep = summarize_pending(conn, llm, pipeline.prompts_dir, limit=pipeline.summarize_limit)
        if rep.failed:
            warnings.append(f"{rep.failed} summaries failed")
        if rep.budget_hit:
            warnings.append("Daily LLM budget reached: some items are not summarized.")

    def rank() -> None:
        rep = rank_items(
            conn,
            pipeline.embedder,
            llm,
            pipeline.profile,
            pipeline.prompts_dir,
            since_days=pipeline.since_days,
        )
        if rep.budget_hit:
            warnings.append(
                "Daily LLM budget reached: lower-ranked items use embedding score only."
            )
        if rep.llm_failed:
            warnings.append(f"{rep.llm_failed} LLM scores failed")

    _stage("summarize", warnings, summarize)
    _stage("rank", warnings, rank)
    brief = build_brief(
        conn,
        top_n=pipeline.profile.top_n,
        since_days=pipeline.since_days,
        warnings=warnings,
        tokens=llm.tokens_used if llm else 0,
        cost_usd=llm.cost_usd if llm else 0.0,
    )
    _stage("deliver", warnings, lambda: _deliver(conn, client, pipeline, brief, run_id, warnings))
    return brief
