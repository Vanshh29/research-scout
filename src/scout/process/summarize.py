"""Summarize ingested items with a versioned prompt and validate the model's JSON."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator

from scout.llm.base import BudgetExceeded, LLMError
from scout.llm.client import LLMClient
from scout.log import get_logger
from scout.process.sanitize import wrap_document

log = get_logger("summarize")

PROMPT_NAME = "summarize_v1"
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def loads_lenient(text: str) -> object:
    """json.loads, retrying once with stray backslashes (e.g. LaTeX like \\alpha) escaped."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE_RE.sub(r"\\\\", text))


class Summary(BaseModel):
    tldr: str
    key_points: list[str] = []
    entities: list[str] = []

    @field_validator("tldr", mode="before")
    @classmethod
    def _clip_tldr(cls, v: object) -> object:
        return v.strip()[:300] if isinstance(v, str) else v

    @field_validator("key_points", mode="before")
    @classmethod
    def _clip_points(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(p).strip()[:240] for p in v[:5] if str(p).strip()]
        return v

    @field_validator("entities", mode="before")
    @classmethod
    def _clip_entities(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(e).strip()[:80] for e in v[:10] if str(e).strip()]
        return v


class SummarizeReport(BaseModel):
    done: int = 0
    failed: int = 0
    cached: int = 0
    budget_hit: bool = False


def load_prompt(prompts_dir: Path, name: str = PROMPT_NAME) -> str:
    return (prompts_dir / f"{name}.md").read_text(encoding="utf-8")


def build_user_prompt(item_id: str, source: str, title: str, body: str) -> str:
    doc = wrap_document(f"{title}\n\n{body}", doc_id=item_id, source=source)
    return f"Summarize this item as the JSON object described.\n\n{doc}"


def parse_summary(text: str) -> Summary:
    """Parse the model reply into a Summary; raises ValueError if it is not valid JSON."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        return Summary.model_validate(loads_lenient(cleaned))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid summary JSON: {exc}") from exc


def pending_items(
    conn: sqlite3.Connection, *, prompt_version: str = PROMPT_NAME, limit: int | None = None
) -> list[sqlite3.Row]:
    """Items with no summary for this prompt version, or whose content changed since."""
    sql = """SELECT i.id, i.source, i.title, i.body, i.content_hash FROM items i
             LEFT JOIN summaries s ON s.item_id = i.id AND s.prompt_version = ?
             WHERE s.item_id IS NULL OR s.content_hash != i.content_hash
             ORDER BY i.first_seen_at DESC, i.id"""
    params: list[object] = [prompt_version]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def summarize_pending(
    conn: sqlite3.Connection, client: LLMClient, prompts_dir: Path, *, limit: int | None = None
) -> SummarizeReport:
    """Summarize items that need it. One bad item never stops the run; a spent budget does."""
    system = load_prompt(prompts_dir)
    report = SummarizeReport()
    for row in pending_items(conn, limit=limit):
        prompt = build_user_prompt(row["id"], row["source"], row["title"], row["body"])
        try:
            result = client.complete(
                system=system,
                prompt=prompt,
                prompt_version=PROMPT_NAME,
                content_hash=row["content_hash"],
                validate=parse_summary,
            )
            summary = parse_summary(result.text)
        except BudgetExceeded:
            report.budget_hit = True
            break
        except (LLMError, ValueError) as exc:
            report.failed += 1
            log.warning("summary failed", extra={"item_id": row["id"], "error": str(exc)})
            continue
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO summaries (item_id, prompt_version, model, content_hash,
                       tldr, key_points, entities, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    PROMPT_NAME,
                    result.model,
                    row["content_hash"],
                    summary.tldr,
                    json.dumps(summary.key_points),
                    json.dumps(summary.entities),
                    datetime.now(UTC).isoformat(),
                ),
            )
        report.done += 1
        report.cached += int(result.cached)
    return report
