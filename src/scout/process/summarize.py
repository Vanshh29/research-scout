"""Summarize ingested items with a versioned prompt and validate the model's JSON."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from scout.llm.base import BudgetExceeded, LLMError
from scout.llm.client import LLMClient
from scout.log import get_logger
from scout.process.sanitize import wrap_document

log = get_logger("summarize")

PROMPT_NAME = "summarize_v2"
ENTITY_TYPES = ("Concept", "Method", "Repo", "Org")
RELATION_TYPES = ("builds_on", "implements", "mentions", "related_to")
MAX_ENTITIES = 12
MAX_RELATIONS = 12
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def loads_lenient(text: str) -> object:
    """json.loads, retrying once with stray backslashes (e.g. LaTeX like \\alpha) escaped."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE_RE.sub(r"\\\\", text))


def _canonical(value: str, choices: tuple[str, ...], default: str) -> str:
    """Map a model-supplied label onto the allowed set (case-insensitive); unknown -> default."""
    return next((c for c in choices if c.casefold() == value.strip().casefold()), default)


class ExtractedEntity(BaseModel):
    name: str
    type: str = "Concept"
    aliases: list[str] = []

    @field_validator("name", mode="before")
    @classmethod
    def _clip_name(cls, v: object) -> object:
        return v.strip()[:80] if isinstance(v, str) else v

    @field_validator("type", mode="before")
    @classmethod
    def _known_type(cls, v: object) -> object:
        return _canonical(v, ENTITY_TYPES, "Concept") if isinstance(v, str) else "Concept"

    @field_validator("aliases", mode="before")
    @classmethod
    def _clip_aliases(cls, v: object) -> object:
        if isinstance(v, list):
            return [str(a).strip()[:80] for a in v[:5] if str(a).strip()]
        return []


class Relation(BaseModel):
    src: str
    relation: str = "related_to"
    dst: str

    @field_validator("relation", mode="before")
    @classmethod
    def _known_relation(cls, v: object) -> object:
        return _canonical(v, RELATION_TYPES, "related_to") if isinstance(v, str) else "related_to"


class Summary(BaseModel):
    tldr: str
    key_points: list[str] = []
    entities: list[ExtractedEntity] = []
    relations: list[Relation] = []

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
    def _coerce_entities(cls, v: object) -> object:
        """Accept v1-style plain names as well as v2 objects; drop junk entries, not the summary."""
        if not isinstance(v, list):
            return []
        out: list[object] = []
        for e in v:
            if isinstance(e, str):
                e = {"name": e}
            if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip():
                out.append(e)
        return out

    @field_validator("relations", mode="before")
    @classmethod
    def _coerce_relations(cls, v: object) -> object:
        if not isinstance(v, list):
            return []
        return [
            r
            for r in v
            if isinstance(r, dict)
            and isinstance(r.get("src"), str)
            and isinstance(r.get("dst"), str)
        ]

    @model_validator(mode="after")
    def _consistent_graph(self) -> "Summary":
        """Dedupe entities, cap both lists, and keep only relations between listed entities."""
        seen: dict[str, ExtractedEntity] = {}
        for e in self.entities:
            seen.setdefault(e.name.casefold(), e)
        self.entities = list(seen.values())[:MAX_ENTITIES]
        names = {e.name.casefold(): e.name for e in self.entities}
        kept: list[Relation] = []
        for r in self.relations:
            src, dst = names.get(r.src.strip().casefold()), names.get(r.dst.strip().casefold())
            if src and dst and src != dst:
                kept.append(Relation(src=src, relation=r.relation, dst=dst))
        self.relations = kept[:MAX_RELATIONS]
        return self

    def graph_json(self) -> str:
        return json.dumps(
            {
                "entities": [e.model_dump() for e in self.entities],
                "relations": [r.model_dump() for r in self.relations],
            }
        )


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
                       tldr, key_points, entities, graph_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    PROMPT_NAME,
                    result.model,
                    row["content_hash"],
                    summary.tldr,
                    json.dumps(summary.key_points),
                    json.dumps([e.name for e in summary.entities]),
                    summary.graph_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        report.done += 1
        report.cached += int(result.cached)
    return report
