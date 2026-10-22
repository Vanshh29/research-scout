"""Terminal labeling loop. Labels are typed by a human; nothing here calls an LLM."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple


class LabelTarget(NamedTuple):
    item_id: str
    title: str
    source: str
    url: str
    body: str
    tldr: str | None
    key_points: list[str]
    entities: list[str]


def next_unlabeled(conn: sqlite3.Connection, n: int) -> list[LabelTarget]:
    """Unlabeled ranked items in a fixed pseudo-random order, so the sample is not biased
    toward high-ranked items (which would flatter precision@k)."""
    rows = conn.execute(
        """SELECT i.id, i.title, i.source, i.url, i.body,
                  s.tldr, s.key_points, s.entities
           FROM scores sc JOIN items i ON i.id = sc.item_id
           LEFT JOIN latest_summaries s ON s.item_id = i.id
           WHERE i.id NOT IN (SELECT item_id FROM labels)"""
    ).fetchall()
    rows.sort(key=lambda r: hashlib.md5(r["id"].encode()).hexdigest())
    return [
        LabelTarget(
            r["id"],
            r["title"],
            r["source"],
            r["url"],
            r["body"],
            r["tldr"],
            json.loads(r["key_points"]) if r["key_points"] else [],
            json.loads(r["entities"]) if r["entities"] else [],
        )
        for r in rows[:n]
    ]


def save_label(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    relevance: int,
    faithfulness: int | None = None,
    coverage: int | None = None,
    conciseness: int | None = None,
    entities_ok: bool | None = None,
) -> None:
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO labels (item_id, relevance, faithfulness, coverage,
                   conciseness, entities_ok, labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                relevance,
                faithfulness,
                coverage,
                conciseness,
                None if entities_ok is None else int(entities_ok),
                datetime.now(UTC).isoformat(),
            ),
        )


def label_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0])


def _ask_int(ask: Callable[[str], str], prompt: str, low: int, high: int) -> int | str | None:
    """An int in [low, high]; None if skipped with Enter; 'q' to quit."""
    while True:
        raw = ask(prompt).strip().lower()
        if raw == "q":
            return "q"
        if raw == "":
            return None
        if raw.isdigit() and low <= int(raw) <= high:
            return int(raw)


def run_labeling(
    conn: sqlite3.Connection,
    n: int,
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> int:
    """Label up to n items. Relevance is required (Enter skips the item); returns labels saved."""
    saved = 0
    for t in next_unlabeled(conn, n):
        say(f"\n[{label_count(conn)} labeled]  {t.title}  ({t.source})\n{t.url}")
        say(t.body[:700].strip())
        relevance = _ask_int(ask, "relevance 0-3 (Enter skip, q quit): ", 0, 3)
        if relevance == "q":
            break
        if not isinstance(relevance, int):
            continue
        faith = cover = concise = None
        entities_ok: bool | None = None
        if t.tldr:
            say(f"\nSUMMARY: {t.tldr}")
            for p in t.key_points:
                say(f"  - {p}")
            say(f"ENTITIES: {', '.join(t.entities)}")
            vals = [
                _ask_int(ask, f"{name} 1-5 (Enter skip): ", 1, 5)
                for name in ("faithfulness", "coverage", "conciseness")
            ]
            if "q" in vals:
                break
            faith, cover, concise = (v if isinstance(v, int) else None for v in vals)
            ent = ask("entities correct? y/n (Enter skip): ").strip().lower()
            entities_ok = {"y": True, "n": False}.get(ent)
        save_label(
            conn,
            t.item_id,
            relevance=relevance,
            faithfulness=faith,
            coverage=cover,
            conciseness=concise,
            entities_ok=entities_ok,
        )
        saved += 1
    return saved
