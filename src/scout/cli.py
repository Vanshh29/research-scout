"""`scout` command-line interface. Each pipeline stage is its own idempotent command."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from scout.brief import build_brief, record_brief, render_telegram, render_text
from scout.config import (
    Settings,
    SourcesConfig,
    load_llm_config,
    load_profile,
    load_sources,
)
from scout.deliver.telegram import DeliveryError, send_message
from scout.eval.labeler_cli import label_count, run_labeling
from scout.graph import queries as graph_queries
from scout.graph.build import build_graph, load_aliases
from scout.ingest.base import Source
from scout.ingest.http import make_client
from scout.ingest.registry import build_sources
from scout.ingest.run import SourceReport, ingest_sources
from scout.llm.base import LLMError
from scout.llm.client import LLMClient, make_provider
from scout.log import configure_logging
from scout.process.summarize import summarize_pending
from scout.rank.embed_score import embed_items
from scout.rank.embedder import Embedder, FastEmbedder, HashEmbedder
from scout.rank.hybrid import rank_items, recent_items
from scout.scheduler.run_daily import Pipeline, run_daily
from scout.store.db import connect, count_items, migrate
from scout.store.db import snapshot as write_snapshot
from scout.store.runs import last_run, source_statuses
from scout.store.vectors import Backend, Hit, open_index, similar

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


def _build_pipeline(settings: Settings, conn: sqlite3.Connection) -> Pipeline:
    llm_cfg = load_llm_config(settings.config_dir / "llm.yaml")
    llm: LLMClient | None = None
    note: str | None = None
    try:
        llm = LLMClient(conn, make_provider(settings, llm_cfg), llm_cfg)
    except LLMError as exc:
        note = f"LLM unavailable ({exc}); summaries skipped, ranking is embedding-only."
    return Pipeline(
        embedder=_make_embedder(settings),
        profile=load_profile(settings.config_dir / "profile.yaml"),
        prompts_dir=settings.prompts_dir,
        llm=llm,
        llm_note=note,
        telegram_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        webhook_url=settings.n8n_webhook_url,
    )


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
    pipeline = _build_pipeline(settings, conn)
    with make_client(user_agent=cfg.http.user_agent, timeout_s=cfg.http.timeout_s) as client:
        summary = run_daily(conn, sources, client, trigger=trigger, pipeline=pipeline)

    _echo_reports(summary.reports)
    if summary.brief is not None:
        typer.echo(render_text(summary.brief))
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


@app.command("build-graph")
def build_graph_cmd() -> None:
    """Build the knowledge graph from summaries (only new or re-summarized items)."""
    settings = Settings()
    configure_logging(settings.log_level)
    conn = connect(settings.db_path)
    migrate(conn)
    report = build_graph(conn, load_aliases(settings.config_dir / "aliases.yaml"))
    typer.echo(
        f"items={report.items} nodes_added={report.entities_added} "
        f"edges_added={report.edges_added} skipped={report.skipped}"
    )


graph_app = typer.Typer(help="Query the knowledge graph.")
app.add_typer(graph_app, name="graph")


def _graph_conn() -> sqlite3.Connection:
    settings = Settings()
    configure_logging(settings.log_level)
    conn = connect(settings.db_path)
    migrate(conn)
    return conn


@graph_app.command("neighbors")
def graph_neighbors(
    name: Annotated[str, typer.Argument(help="Concept name or alias.")],
    depth: Annotated[int, typer.Option(help="Hops from the concept.")] = 1,
    limit: Annotated[int, typer.Option(help="Max rows.")] = 25,
) -> None:
    """What a concept is connected to: related concepts, papers, repos, posts."""
    found = graph_queries.neighbors(_graph_conn(), name, depth)
    if not found:
        typer.echo(f"nothing found for {name!r}")
    for n in found[:limit]:
        via = f"  [{', '.join(n.relations)}]" if n.relations else ""
        typer.echo(f"{n.distance}  {n.node.type:<8} {n.node.name[:70]}{via}")


@graph_app.command("path")
def graph_path(
    a: Annotated[str, typer.Argument(help="Start concept.")],
    b: Annotated[str, typer.Argument(help="End concept.")],
) -> None:
    """Shortest chain of links between two concepts."""
    steps = graph_queries.shortest_path(_graph_conn(), a, b)
    if steps is None:
        typer.echo("no path (unknown concept or not connected)")
        return
    for node, label in steps:
        typer.echo(f"{node.type:<8} {node.name[:70]}")
        if label:
            typer.echo(f"   --{label}-->")


@graph_app.command("trending")
def graph_trending(
    days: Annotated[int, typer.Option(help="Window length; compared with the window before.")] = 7,
    min_count: Annotated[int, typer.Option(help="Min mentions in the recent window.")] = 2,
    limit: Annotated[int, typer.Option(help="Max rows.")] = 10,
) -> None:
    """Concepts mentioned more in the last N days than in the N days before."""
    rows = graph_queries.trending(_graph_conn(), days=days, min_count=min_count, limit=limit)
    if not rows:
        typer.echo("nothing trending yet (needs concepts mentioned by >= min-count items)")
    for t in rows:
        tag = "new" if t.is_new else f"was {t.previous}"
        typer.echo(f"{t.recent:>3}  {t.node.name[:50]:<50} {t.node.type:<8} {tag}")


@graph_app.command("clusters")
def graph_clusters(
    min_size: Annotated[int, typer.Option(help="Smallest cluster to show.")] = 3,
) -> None:
    """Groups of concepts that tend to be mentioned together."""
    found = graph_queries.clusters(_graph_conn(), min_size=min_size)
    if not found:
        typer.echo("no clusters yet (needs concepts that co-occur in items)")
    for i, c in enumerate(found, 1):
        typer.echo(f"{i}. ({c.size}) {', '.join(c.members[:8])}")


def _make_embedder(settings: Settings) -> Embedder:
    return HashEmbedder() if settings.llm_provider == "fake" else FastEmbedder()


@app.command()
def embed(
    since_days: Annotated[int, typer.Option(help="Only items first seen this recently.")] = 7,
) -> None:
    """Embed recent items locally (no API key needed)."""
    from datetime import UTC, datetime, timedelta

    settings = Settings()
    configure_logging(settings.log_level)
    conn = connect(settings.db_path)
    migrate(conn)
    rows = recent_items(conn, datetime.now(UTC) - timedelta(days=since_days))
    n = embed_items(conn, _make_embedder(settings), [r["id"] for r in rows])
    typer.echo(f"embedded={n} of {len(rows)} recent items")


def _echo_hits(conn: sqlite3.Connection, hits: list[Hit]) -> None:
    if not hits:
        typer.echo("no results (run `scout embed` first?)")
    for h in hits:
        row = conn.execute("SELECT title, source FROM items WHERE id = ?", (h.item_id,)).fetchone()
        title, source = (row["title"], row["source"]) if row else ("?", "?")
        typer.echo(f"{h.score:.3f}  {h.item_id:<28} {source:<8} {title[:80]}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Free-text query.")],
    k: Annotated[int, typer.Option("-k", help="How many results.")] = 10,
    backend: Annotated[Backend, typer.Option(help="auto, vec0 or numpy.")] = "auto",
) -> None:
    """Semantic search over embedded items."""
    settings = Settings()
    configure_logging(settings.log_level)
    conn = connect(settings.db_path)
    migrate(conn)
    embedder = _make_embedder(settings)
    index = open_index(
        conn, embedder.name, settings.db_path.with_name("vectors.db"), backend=backend
    )
    _echo_hits(conn, index.search(embedder.embed([query])[0], k))


@app.command("similar")
def similar_cmd(
    item_id: Annotated[str, typer.Argument(help="Item id, e.g. arxiv:2401.00001.")],
    k: Annotated[int, typer.Option("-k", help="How many results.")] = 10,
    backend: Annotated[Backend, typer.Option(help="auto, vec0 or numpy.")] = "auto",
) -> None:
    """Items most like the given one."""
    settings = Settings()
    configure_logging(settings.log_level)
    conn = connect(settings.db_path)
    migrate(conn)
    embedder = _make_embedder(settings)
    index = open_index(
        conn, embedder.name, settings.db_path.with_name("vectors.db"), backend=backend
    )
    _echo_hits(conn, similar(conn, index, item_id, embedder.name, k))


@app.command()
def rank(
    since_days: Annotated[int, typer.Option(help="Only items first seen this recently.")] = 7,
    no_llm: Annotated[bool, typer.Option(help="Embedding-only ranking.")] = False,
) -> None:
    """Score recent items: embedding pass, then LLM rubric for the top-K, blended."""
    settings = Settings()
    configure_logging(settings.log_level)
    profile = load_profile(settings.config_dir / "profile.yaml")
    llm_cfg = load_llm_config(settings.config_dir / "llm.yaml")
    conn = connect(settings.db_path)
    migrate(conn)
    client: LLMClient | None = None
    if not no_llm:
        try:
            client = LLMClient(conn, make_provider(settings, llm_cfg), llm_cfg)
        except LLMError as exc:
            typer.echo(f"no LLM scoring ({exc}); falling back to embedding-only", err=True)
    report = rank_items(
        conn, _make_embedder(settings), client, profile, settings.prompts_dir, since_days=since_days
    )
    typer.echo(
        f"candidates={report.candidates} embedded={report.embedded} llm_scored={report.llm_scored} "
        f"llm_failed={report.llm_failed}" + ("  BUDGET HIT" if report.budget_hit else "")
    )


@app.command()
def brief(
    send: Annotated[
        bool, typer.Option(help="Deliver via Telegram and mark items as shown.")
    ] = False,
    since_days: Annotated[int, typer.Option(help="Look back this many days.")] = 7,
) -> None:
    """Print today's top items from the ranked set (preview only unless --send)."""
    settings = Settings()
    configure_logging(settings.log_level)
    profile = load_profile(settings.config_dir / "profile.yaml")
    conn = connect(settings.db_path)
    migrate(conn)
    b = build_brief(conn, top_n=profile.top_n, since_days=since_days)
    typer.echo(render_text(b))
    if not send:
        return
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        typer.echo("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; nothing sent", err=True)
        raise typer.Exit(1)
    try:
        with make_client(user_agent="research-scout/0.1", timeout_s=30.0) as client:
            send_message(
                client,
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                text=render_telegram(b),
            )
    except DeliveryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    record_brief(conn, b, run_id=None)
    typer.echo("sent")


@app.command()
def label(n: Annotated[int, typer.Option(help="How many items this session.")] = 10) -> None:
    """Hand-label items (relevance 0-3, summary quality 1-5). Ground truth for the eval."""
    settings = Settings()
    conn = connect(settings.db_path)
    migrate(conn)
    saved = run_labeling(conn, n, say=typer.echo)
    typer.echo(f"saved {saved} labels; {label_count(conn)} total")
