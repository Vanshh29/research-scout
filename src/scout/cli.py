"""`scout` command-line interface. Each pipeline stage is its own idempotent command."""

from collections.abc import Sequence
from typing import Annotated

import typer

from scout.config import Settings, SourcesConfig, load_sources
from scout.ingest.base import Source
from scout.ingest.http import make_client
from scout.ingest.registry import build_sources
from scout.ingest.run import SourceReport, ingest_sources
from scout.log import configure_logging
from scout.scheduler.run_daily import run_daily
from scout.store.db import connect, count_items, migrate
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
