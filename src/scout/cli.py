"""`scout` command-line interface. Each pipeline stage is its own idempotent command."""

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from scout.config import Settings, SourcesConfig, load_llm_config, load_sources
from scout.ingest.base import Source
from scout.ingest.http import make_client
from scout.ingest.registry import build_sources
from scout.ingest.run import SourceReport, ingest_sources
from scout.llm.base import LLMError
from scout.llm.client import LLMClient, make_provider
from scout.log import configure_logging
from scout.process.summarize import summarize_pending
from scout.scheduler.run_daily import run_daily
from scout.store.db import connect, count_items, migrate
from scout.store.db import snapshot as write_snapshot
from scout.store.runs import last_run, source_statuses

app = typer.Typer(help="Research Scout: daily research ingestion, ranking, and evaluation.")

SourceOption = Annotated[
    list[str] | None, typer.Option("--source", "-s", help="Only run these sources.")
]


@app.callback()
def main() -> None:
    """Research Scout CLI."""


def _select_sources(cfg: SourcesConfig, settings: Settings, only: list[str] | None) -> list[Source]:
    sources = build_sources(cfg, settings)
    return [s for s in sources if s.name in only] if only else sources


def _echo_reports(reports: Sequence[SourceReport]) -> None:
    for r in reports:
        line = (
            f"{r.source:<8} {r.status:<6} fetched={r.fetched} new={r.new} "
            f"updated={r.updated} unchanged={r.unchanged} {r.latency_ms}ms"
        )
        typer.echo(line + (f"  error={r.error}" if r.error else ""))


@app.command()
def ingest(source: SourceOption = None) -> None:
    """Fetch new items from all configured sources into the database."""
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_sources(settings.config_dir / "sources.yaml")
    sources = _select_sources(cfg, settings, source)

    conn = connect(settings.db_path)
    migrate(conn)
    with make_client(user_agent=cfg.http.user_agent, timeout_s=cfg.http.timeout_s) as client:
        reports = ingest_sources(conn, sources, client)

    _echo_reports(reports)
    typer.echo(f"total items in db: {count_items(conn)}")
    if reports and all(r.status == "failed" for r in reports):
        raise typer.Exit(code=1)


@app.command("run-daily")
def run_daily_cmd(
    source: SourceOption = None,
    trigger: Annotated[
        str, typer.Option(help="Recorded on the run (manual, cron, ...).")
    ] = "manual",
) -> None:
    """Run every pipeline stage once and record the run. Exits 1 only if the run failed."""
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_sources(settings.config_dir / "sources.yaml")
    sources = _select_sources(cfg, settings, source)

    conn = connect(settings.db_path)
    migrate(conn)
    with make_client(user_agent=cfg.http.user_agent, timeout_s=cfg.http.timeout_s) as client:
        summary = run_daily(conn, sources, client, trigger=trigger)

    _echo_reports(summary.reports)
    typer.echo(
        f"run #{summary.run_id} {summary.status}: fetched={summary.items_fetched} "
        f"new={summary.items_new} {summary.latency_ms}ms; total items in db: {count_items(conn)}"
    )
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show the last recorded run and per-source health."""
    settings = Settings()
    conn = connect(settings.db_path)
    migrate(conn)
    run = last_run(conn)
    if run is None:
        typer.echo("no runs recorded yet")
        return
    typer.echo(
        f"run #{run['id']} {run['status']} started={run['started_at']} "
        f"fetched={run['items_fetched']} new={run['items_new']} "
        f"failed_sources={run['items_failed']} {run['latency_ms']}ms"
    )
    for s in source_statuses(conn, run["id"]):
        typer.echo(
            f"  {s['source']:<8} {s['status']:<6} fetched={s['items_fetched']} "
            f"new={s['items_new']}" + (f"  error={s['error']}" if s["error"] else "")
        )


@app.command()
def snapshot(out: Annotated[Path, typer.Option(help="Where to write the copy.")]) -> None:
    """Write a standalone copy of the database (published for the dashboard)."""
    settings = Settings()
    conn = connect(settings.db_path)
    write_snapshot(conn, out)
    typer.echo(f"snapshot written to {out} ({out.stat().st_size} bytes)")


@app.command()
def summarize(
    limit: Annotated[int | None, typer.Option(help="Max items this run.")] = None,
) -> None:
    """Summarize items that have no summary yet (cached, budget-guarded)."""
    settings = Settings()
    configure_logging(settings.log_level)
    llm_cfg = load_llm_config(settings.config_dir / "llm.yaml")
    conn = connect(settings.db_path)
    migrate(conn)
    try:
        provider = make_provider(settings, llm_cfg)
    except LLMError as exc:
        typer.echo(f"cannot summarize: {exc}", err=True)
        raise typer.Exit(1) from exc
    client = LLMClient(conn, provider, llm_cfg)
    report = summarize_pending(conn, client, settings.prompts_dir, limit=limit)
    typer.echo(
        f"summarized={report.done} failed={report.failed} cached={report.cached} "
        f"llm_calls={client.calls} tokens={client.tokens_used}"
        + ("  BUDGET HIT" if report.budget_hit else "")
    )
