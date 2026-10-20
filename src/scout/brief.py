"""Build the daily brief from ranked items and render it for the terminal and Telegram."""

import html
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from scout.process.sanitize import is_http_url

TELEGRAM_LIMIT = 3800  # Telegram caps messages at 4096 characters; leave headroom


class BriefEntry(BaseModel):
    rank: int
    item_id: str
    title: str
    url: str
    source: str
    hybrid: float
    embed_score: float
    llm_score: float | None
    breakdown: dict[str, float] = {}
    reason: str | None = None
    tldr: str | None = None


class Brief(BaseModel):
    date: str
    new_items: int
    entries: list[BriefEntry]
    warnings: list[str] = []
    tokens: int = 0
    cost_usd: float = 0.0


def select_entries(conn: sqlite3.Connection, *, top_n: int, since: datetime) -> list[BriefEntry]:
    """Best-scored recent items that no earlier brief has shown."""
    rows = conn.execute(
        """SELECT i.id, i.title, i.url, i.source, sc.hybrid, sc.embed_score, sc.llm_score,
                  sc.llm_breakdown, sc.llm_reason,
                  (SELECT tldr FROM summaries s WHERE s.item_id = i.id
                   ORDER BY s.created_at DESC LIMIT 1) AS tldr
           FROM scores sc JOIN items i ON i.id = sc.item_id
           WHERE i.first_seen_at >= ? AND i.id NOT IN (SELECT item_id FROM brief_items)
           ORDER BY sc.hybrid DESC, i.id LIMIT ?""",
        (since.isoformat(), top_n),
    ).fetchall()
    return [
        BriefEntry(
            rank=n,
            item_id=r["id"],
            title=r["title"],
            url=r["url"],
            source=r["source"],
            hybrid=r["hybrid"],
            embed_score=r["embed_score"],
            llm_score=r["llm_score"],
            breakdown=json.loads(r["llm_breakdown"]) if r["llm_breakdown"] else {},
            reason=r["llm_reason"],
            tldr=r["tldr"],
        )
        for n, r in enumerate(rows, start=1)
    ]


def build_brief(
    conn: sqlite3.Connection,
    *,
    top_n: int,
    since_days: int = 7,
    warnings: list[str] | None = None,
    tokens: int = 0,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> Brief:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=since_days)
    entries = select_entries(conn, top_n=top_n, since=since)
    new_items = conn.execute(
        "SELECT COUNT(*) FROM items WHERE first_seen_at >= ?", (since.isoformat(),)
    ).fetchone()[0]
    warns = list(warnings or [])
    if entries and all(e.llm_score is None for e in entries):
        warns.append("LLM scoring unavailable: ranked by embedding similarity only.")
    if not entries:
        warns.append("No new ranked items since the last brief.")
    return Brief(
        date=now.strftime("%Y-%m-%d"),
        new_items=new_items,
        entries=entries,
        warnings=warns,
        tokens=tokens,
        cost_usd=cost_usd,
    )


def record_brief(conn: sqlite3.Connection, brief: Brief, *, run_id: int | None) -> int:
    """Persist the brief and mark its items as shown."""
    with conn:
        cur = conn.execute(
            "INSERT INTO briefs (run_id, created_at, payload) VALUES (?, ?, ?)",
            (run_id, datetime.now(UTC).isoformat(), brief.model_dump_json()),
        )
        assert cur.lastrowid is not None
        conn.executemany(
            "INSERT OR IGNORE INTO brief_items (item_id, brief_id, rank) VALUES (?, ?, ?)",
            [(e.item_id, cur.lastrowid, e.rank) for e in brief.entries],
        )
    return cur.lastrowid


def _score_line(e: BriefEntry) -> str:
    llm = "n/a" if e.llm_score is None else f"{e.llm_score:.2f}"
    return f"score {e.hybrid:.2f} (embedding {e.embed_score:.2f}, llm {llm})"


def render_text(brief: Brief) -> str:
    lines = [f"Research Scout brief {brief.date}: {len(brief.entries)} of {brief.new_items} new"]
    for e in brief.entries:
        lines.append(f"\n{e.rank}. {e.title}  [{e.source}]\n   {e.url}\n   {_score_line(e)}")
        if e.breakdown:
            lines.append("   " + ", ".join(f"{k} {v:.2f}" for k, v in e.breakdown.items()))
        if e.tldr:
            lines.append(f"   {e.tldr}")
        if e.reason:
            lines.append(f"   why: {e.reason}")
    for w in brief.warnings:
        lines.append(f"\n! {w}")
    return "\n".join(lines)


def render_telegram(brief: Brief) -> str:
    """Telegram HTML. All fetched text is escaped: it is untrusted."""
    head = (
        f"<b>Research Scout {html.escape(brief.date)}</b>\n"
        f"{len(brief.entries)} of {brief.new_items} new items\n"
    )
    foot = "".join(f"\n⚠ {html.escape(w)}" for w in brief.warnings)
    blocks: list[str] = []
    used = len(head) + len(foot)
    for e in brief.entries:
        title = html.escape(e.title)
        link = (
            f'<a href="{html.escape(e.url, quote=True)}">{title}</a>'
            if is_http_url(e.url)
            else title
        )
        block = f"\n{e.rank}. {link} ({html.escape(e.source)}) · {e.hybrid:.2f}"
        if e.tldr:
            block += f"\n{html.escape(e.tldr)}"
        if e.reason:
            block += f"\n<i>{html.escape(e.reason)}</i>"
        if used + len(block) > TELEGRAM_LIMIT:
            blocks.append(f"\n+{len(brief.entries) - len(blocks)} more on the dashboard")
            break
        blocks.append(block)
        used += len(block)
    return head + "".join(blocks) + foot
