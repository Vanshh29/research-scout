"""`scout` command-line interface. Each pipeline stage is its own idempotent command."""

from typing import Annotated

import typer

from scout.config import Settings, load_sources
from scout.ingest.arxiv import ArxivSource
from scout.ingest.base import Source
from scout.ingest.http import make_client
from scout.ingest.run import ingest_sources
from scout.log import configure_logging
from scout.store.db import connect, count_items, migrate

app = typer.Typer(help="Research Scout: daily research ingestion, ranking, and evaluation.")


@app.callback()
def main() -> None:
    """Research Scout CLI."""


@app.command()
def ingest(
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Only run these sources.")
    ] = None,
) -> None:
    """Fetch new items from all configured sources into the database."""
    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_sources(settings.config_dir / "sources.yaml")

    sources: list[Source] = [ArxivSource(cfg.arxiv, max_retries=cfg.http.max_retries)]
    if source:
        sources = [s for s in sources if s.name in source]

    conn = connect(settings.db_path)
    migrate(conn)
    with make_client(user_agent=cfg.http.user_agent, timeout_s=cfg.http.timeout_s) as client:
        reports = ingest_sources(conn, sources, client)

    for r in reports:
        line = (
            f"{r.source:<8} {r.status:<6} fetched={r.fetched} new={r.new} "
            f"updated={r.updated} unchanged={r.unchanged} {r.latency_ms}ms"
        )
        typer.echo(line + (f"  error={r.error}" if r.error else ""))
    typer.echo(f"total items in db: {count_items(conn)}")
    if reports and all(r.status == "failed" for r in reports):
        raise typer.Exit(code=1)
