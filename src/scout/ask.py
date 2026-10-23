"""Grounded question answering over the collected items, with validated citations.

Retrieval picks a few items, each is shown to the model as a delimited document (summary, not the
full body), and the reply must cite documents by number. Citations the model invented are dropped,
and an answer left with none is replaced by a fixed fallback, so no uncited claim is shown.
"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from scout.llm.base import BudgetExceeded, LLMError
from scout.llm.client import LLMClient
from scout.log import get_logger
from scout.process.sanitize import sanitize_text, wrap_document
from scout.process.summarize import loads_lenient
from scout.search import Searcher, SearchHit

log = get_logger("ask")

PROMPT_NAME = "ask_v1"
TOP_K = 6
MAX_ANSWER_CHARS = 1500
FALLBACK = "I could not find this in the collected items."
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_MARKER_RE = re.compile(r"\[(\d+)\]")


class AskRefusedError(Exception):
    """Over today's Ask or token budget; nothing was sent to the provider."""


class AskUnavailableError(Exception):
    """The provider failed."""


class AskUnusableError(Exception):
    """The provider replied, but not with a usable answer."""


@dataclass(frozen=True)
class Source:
    n: int
    item_id: str
    title: str
    url: str


@dataclass(frozen=True)
class Answer:
    answer: str
    grounded: bool
    sources: list[Source]  # the documents actually cited
    retrieved: list[Source]  # everything the model was shown
    cached: bool = False


class RawAnswer(BaseModel):
    answer: str
    citations: list[int]


def parse_answer(text: str) -> RawAnswer:
    """Parse the reply; citations that are not integers are skipped. Raises ValueError."""
    try:
        data = loads_lenient(_FENCE_RE.sub("", text.strip()))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid answer JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("answer must be a JSON object")
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer is missing")
    numbers: list[int] = []
    raw = data.get("citations")
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, int) and not isinstance(c, bool):
            numbers.append(c)
        elif isinstance(c, str) and c.strip().isdigit():
            numbers.append(int(c))
    return RawAnswer(answer=answer.strip()[:MAX_ANSWER_CHARS], citations=numbers)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _calls_today(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT calls FROM ask_usage WHERE day = ?", (_today(),)).fetchone()
    return 0 if row is None else int(row["calls"])


def _count_call(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "INSERT INTO ask_usage (day, calls) VALUES (?, 1)"
            " ON CONFLICT (day) DO UPDATE SET calls = calls + 1",
            (_today(),),
        )


def _escape_tags(text: str) -> str:
    return re.sub(r"<(/?)\s*(question|document)", r"&lt;\1\2", text, flags=re.IGNORECASE)


def _document_text(conn: sqlite3.Connection, hit: SearchHit) -> str:
    """Title plus the stored summary; items without one fall back to the start of the body."""
    row = conn.execute(
        """SELECT i.body, s.tldr, s.key_points FROM items i
           LEFT JOIN latest_summaries s ON s.item_id = i.id WHERE i.id = ?""",
        (hit.item_id,),
    ).fetchone()
    if row is not None and row["tldr"]:
        points = json.loads(row["key_points"] or "[]")
        lines = [row["tldr"], *[f"- {p}" for p in points[:5]]]
    else:
        lines = [(row["body"] if row else "")[:400]]
    return sanitize_text(f"{hit.title}\n" + "\n".join(lines))


def build_prompt(question: str, docs: list[tuple[SearchHit, str]]) -> str:
    blocks = [
        wrap_document(_escape_tags(text), doc_id=str(n), source=hit.source)
        for n, (hit, text) in enumerate(docs, start=1)
    ]
    safe_question = _escape_tags(sanitize_text(question)).replace("<", "&lt;").replace(">", "&gt;")
    return (
        "Answer the question using only the documents.\n\n"
        f"<question>\n{safe_question}\n</question>\n\n" + "\n\n".join(blocks)
    )


def _source(n: int, hit: SearchHit) -> Source:
    return Source(n, hit.item_id, hit.title, hit.url)


def _valid_numbers(raw: RawAnswer, count: int) -> list[int]:
    wanted = [*raw.citations, *(int(m) for m in _MARKER_RE.findall(raw.answer))]
    seen: list[int] = []
    for n in wanted:
        if 1 <= n <= count and n not in seen:
            seen.append(n)
    return sorted(seen)


def _strip_invalid_markers(text: str, valid: list[int]) -> str:
    kept = _MARKER_RE.sub(lambda m: m.group(0) if int(m.group(1)) in valid else "", text)
    return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", kept)).strip()


def ask(
    conn: sqlite3.Connection,
    client: LLMClient,
    searcher: Searcher,
    question: str,
    prompts_dir: Path,
    *,
    daily_cap: int,
) -> Answer:
    hits = searcher.search(conn, question, TOP_K).hits
    if not hits:
        return Answer(FALLBACK, False, [], [])
    docs = [(h, _document_text(conn, h)) for h in hits]
    retrieved = [_source(n, h) for n, (h, _) in enumerate(docs, start=1)]
    prompt = build_prompt(question, docs)
    norm_question = " ".join(question.lower().split())
    blocks = prompt.split("</question>", 1)[1]
    content_hash = hashlib.sha256(f"{norm_question}\n{blocks}".encode()).hexdigest()

    if not client.is_cached(PROMPT_NAME, content_hash) and _calls_today(conn) >= daily_cap:
        raise AskRefusedError(f"Daily Ask limit reached ({daily_cap} questions).")
    system = (prompts_dir / f"{PROMPT_NAME}.md").read_text(encoding="utf-8")
    calls_before = client.calls
    try:
        result = client.complete(
            system=system,
            prompt=prompt,
            prompt_version=PROMPT_NAME,
            content_hash=content_hash,
            validate=parse_answer,
        )
    except BudgetExceeded as exc:
        raise AskRefusedError("The daily token budget is used up.") from exc
    except LLMError as exc:
        raise AskUnavailableError(str(exc)) from exc
    finally:
        if client.calls > calls_before:  # the provider was paid, even if its reply is unusable
            _count_call(conn)
    try:
        raw = parse_answer(result.text)
    except ValueError as exc:
        log.warning("unusable answer", extra={"error": str(exc)})
        raise AskUnusableError("The model returned an answer that could not be read.") from exc

    valid = _valid_numbers(raw, len(docs))
    if not valid:
        return Answer(FALLBACK, False, [], retrieved, cached=result.cached)
    cited = [retrieved[n - 1] for n in valid]
    return Answer(
        _strip_invalid_markers(raw.answer, valid), True, cited, retrieved, cached=result.cached
    )
