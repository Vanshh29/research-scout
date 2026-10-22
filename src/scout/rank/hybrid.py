"""Hybrid ranking: free embedding pass over recent items, LLM rubric for the top-K, blended."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from scout.config import Profile
from scout.llm.base import BudgetExceeded, LLMError
from scout.llm.client import LLMClient
from scout.log import get_logger
from scout.process.summarize import load_prompt
from scout.rank.embed_score import (
    embed_items,
    embedding_score,
    item_text,
    load_vector,
    profile_texts,
)
from scout.rank.embedder import Embedder
from scout.rank.llm_score import PROMPT_NAME, build_score_prompt, parse_rubric

log = get_logger("rank")


class RankReport(BaseModel):
    candidates: int = 0
    embedded: int = 0
    llm_scored: int = 0
    llm_failed: int = 0
    budget_hit: bool = False


def hybrid_score(embed: float, llm: float | None, w_embedding: float, w_llm: float) -> float:
    """Blend the two scores; items without an LLM score keep their embedding score."""
    if llm is None:
        return embed
    return w_embedding * embed + w_llm * llm


def _digest(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    s = conn.execute(
        "SELECT tldr, key_points FROM latest_summaries WHERE item_id = ?",
        (row["id"],),
    ).fetchone()
    if s is None:
        return item_text("", row["body"]).strip()[:1200]
    points = "\n".join(f"- {p}" for p in json.loads(s["key_points"]))
    return f"{s['tldr']}\n{points}".strip()


def recent_items(conn: sqlite3.Connection, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, source, title, body, content_hash FROM items WHERE first_seen_at >= ?"
        " ORDER BY first_seen_at DESC, id",
        (since.isoformat(),),
    ).fetchall()


def _save(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    embed: float,
    llm: float | None,
    breakdown: dict[str, float] | None,
    reason: str | None,
    hybrid: float,
    weights: dict[str, float],
    model: str | None,
) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO scores (item_id, content_hash, embed_score, llm_score,
                   llm_breakdown, llm_reason, hybrid, weights, prompt_version, model, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["content_hash"],
                embed,
                llm,
                json.dumps(breakdown) if breakdown else None,
                reason,
                hybrid,
                json.dumps(weights),
                PROMPT_NAME if llm is not None else None,
                model,
                datetime.now(UTC).isoformat(),
            ),
        )


def rank_items(
    conn: sqlite3.Connection,
    embedder: Embedder,
    client: LLMClient | None,
    profile: Profile,
    prompts_dir: Path,
    *,
    since_days: int = 7,
    now: datetime | None = None,
) -> RankReport:
    """Score recent items. Without a client (no key, or budget gone) the brief falls back to
    embedding-only scores, which is the graceful-degradation path."""
    since = (now or datetime.now(UTC)) - timedelta(days=since_days)
    rows = recent_items(conn, since)
    report = RankReport(candidates=len(rows))
    if not rows:
        return report

    report.embedded = embed_items(conn, embedder, [r["id"] for r in rows])
    profile_vecs = embedder.embed(profile_texts(profile))
    weights = {"embedding": profile.weights.embedding, "llm": profile.weights.llm}

    embed_scores: dict[str, float] = {}
    for row in rows:
        vec = load_vector(conn, row["id"], embedder.name)
        assert vec is not None  # embed_items just stored it
        embed_scores[row["id"]] = embedding_score(vec, profile_vecs, embedder.score_range)

    ranked = sorted(rows, key=lambda r: embed_scores[r["id"]], reverse=True)
    top_ids = {r["id"] for r in ranked[: profile.top_k]}
    system = load_prompt(prompts_dir, PROMPT_NAME) if client else ""

    for row in ranked:
        embed = embed_scores[row["id"]]
        llm: float | None = None
        breakdown: dict[str, float] | None = None
        reason: str | None = None
        if client is not None and row["id"] in top_ids and not report.budget_hit:
            prompt = build_score_prompt(
                profile,
                item_id=row["id"],
                source=row["source"],
                title=row["title"],
                digest=_digest(conn, row),
            )
            try:
                result = client.complete(
                    system=system,
                    prompt=prompt,
                    prompt_version=PROMPT_NAME,
                    content_hash=row["content_hash"],
                    validate=parse_rubric,
                )
                rubric = parse_rubric(result.text)
                llm = rubric.weighted(profile.rubric)
                breakdown = {d: getattr(rubric, d) for d in profile.rubric if hasattr(rubric, d)}
                reason = rubric.reason
                report.llm_scored += 1
            except BudgetExceeded:
                report.budget_hit = True
            except (LLMError, ValueError) as exc:
                report.llm_failed += 1
                log.warning("llm score failed", extra={"item_id": row["id"], "error": str(exc)})
        _save(
            conn,
            row,
            embed=embed,
            llm=llm,
            breakdown=breakdown,
            reason=reason,
            hybrid=hybrid_score(embed, llm, weights["embedding"], weights["llm"]),
            weights=weights,
            model=client.model if client and llm is not None else None,
        )
    return report
