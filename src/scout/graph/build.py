"""Build the knowledge graph from stored summaries. Idempotent: unchanged items are skipped."""

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel

from scout.process.summarize import ExtractedEntity, Relation

ITEM_TYPES = {"arxiv": "Paper", "github": "Repo"}  # anything else (blogs) is a Post
MAX_AUTHORS = 10
_VERSION_RE = re.compile(r"[\s-]v\d+(\.\d+)*$")


def normalize(name: str) -> str:
    """Matching key: case, punctuation and hyphen-vs-space do not matter; a trailing ' v2' does not
    either. Bare numbers are kept ('GPT-4' and 'GPT-4o' stay different)."""
    s = unicodedata.normalize("NFKC", name).casefold().strip()
    return re.sub(r"[^\w]+", " ", _VERSION_RE.sub("", s)).strip()


def load_aliases(path: Path) -> dict[str, str]:
    """Seed file -> {normalized alias: canonical name}. A missing file means no seeds."""
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    seeds: dict[str, str] = {}
    for canonical, aliases in raw.items():
        for alias in aliases or []:
            seeds[normalize(str(alias))] = str(canonical)
    return seeds


class GraphReport(BaseModel):
    items: int = 0
    entities_added: int = 0
    edges_added: int = 0
    skipped: int = 0


@dataclass
class _Namespace:
    """Names and aliases of one kind of node. Every normalized form maps to exactly one entity."""

    owner: dict[str, int] = field(default_factory=dict)
    norm_of: dict[int, str] = field(default_factory=dict)
    aliases_of: dict[int, list[str]] = field(default_factory=dict)

    def add(self, eid: int, norm: str, aliases: list[str]) -> None:
        self.owner[norm] = eid
        self.norm_of[eid] = norm
        self.aliases_of[eid] = aliases
        for a in aliases:
            self.owner.setdefault(normalize(a), eid)


class _Resolver:
    """Finds or creates the entity for a surface form, merging by name, alias, or seed."""

    def __init__(self, conn: sqlite3.Connection, seeds: dict[str, str], now: str) -> None:
        self._conn, self._seeds, self._now = conn, seeds, now
        self._spaces = {"concept": _Namespace(), "person": _Namespace()}
        self.created = 0
        for r in conn.execute(
            "SELECT id, type, norm_name, aliases FROM entities WHERE item_id IS NULL"
        ):
            self._space(r["type"]).add(r["id"], r["norm_name"], json.loads(r["aliases"]))

    def _space(self, type_: str) -> _Namespace:
        return self._spaces["person" if type_ == "Person" else "concept"]

    def resolve(self, name: str, type_: str, aliases: list[str]) -> int | None:
        space = self._space(type_)
        forms = [name, *aliases]
        canonical = self._seeds.get(normalize(name))
        if canonical:  # a seeded alias: the canonical name becomes the display name
            forms, name = [canonical, *forms], canonical
        norm = normalize(name)
        if not norm:
            return None
        eid = space.owner.get(norm)
        if eid is None:
            # Name is new: merge only if all the forms we know point at one existing entity.
            known = {space.owner[n] for f in forms if (n := normalize(f)) in space.owner}
            eid = known.pop() if len(known) == 1 else None
        if eid is None:
            eid = self._insert(name, norm, type_)
            space.add(eid, norm, [])
        self._add_aliases(space, eid, forms)
        return eid

    def _add_aliases(self, space: _Namespace, eid: int, forms: list[str]) -> None:
        """Record new surface forms, skipping any that already belong to another entity."""
        current = space.aliases_of[eid]
        added = []
        for form in forms:
            n = normalize(form)
            if n and n not in space.owner:
                space.owner[n] = eid
                added.append(form)
        if added:
            space.aliases_of[eid] = [*current, *added]
            self._conn.execute(
                "UPDATE entities SET aliases = ? WHERE id = ?",
                (json.dumps(space.aliases_of[eid]), eid),
            )

    def _insert(self, name: str, norm: str, type_: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO entities (type, name, norm_name, first_seen_at) VALUES (?, ?, ?, ?)",
            (type_, name, norm, self._now),
        )
        self.created += 1
        assert cur.lastrowid is not None
        return cur.lastrowid


def _item_node(conn: sqlite3.Connection, item: sqlite3.Row, now: str) -> tuple[int, bool]:
    row = conn.execute("SELECT id FROM entities WHERE item_id = ?", (item["id"],)).fetchone()
    if row is not None:
        conn.execute("UPDATE entities SET name = ? WHERE id = ?", (item["title"], row["id"]))
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO entities (type, name, norm_name, item_id, first_seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (ITEM_TYPES.get(item["source"], "Post"), item["title"], item["id"], item["id"], now),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid, True


def _pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.id, i.source, i.title, i.authors, s.graph_json, s.created_at AS summary_at
           FROM latest_summaries s JOIN items i ON i.id = s.item_id
           LEFT JOIN graph_built g ON g.item_id = s.item_id
           WHERE g.item_id IS NULL OR g.summary_created_at != s.created_at
           ORDER BY i.first_seen_at, i.id"""
    ).fetchall()


def build_graph(conn: sqlite3.Connection, seeds: dict[str, str] | None = None) -> GraphReport:
    """Add every summarized item that is new or re-summarized since the last build."""
    report = GraphReport(skipped=0)
    now = datetime.now(UTC).isoformat()
    todo = _pending(conn)
    total = conn.execute("SELECT COUNT(*) FROM latest_summaries").fetchone()[0]
    report.skipped = total - len(todo)
    with conn:
        resolver = _Resolver(conn, seeds or {}, now)
        for item in todo:
            edges = _edges_for(conn, resolver, item, now)
            conn.execute("DELETE FROM edges WHERE item_id = ?", (item["id"],))
            for src, dst, relation in edges:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO edges (src, dst, relation, item_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (src, dst, relation, item["id"], now),
                )
                report.edges_added += cur.rowcount
            conn.execute(
                "INSERT OR REPLACE INTO graph_built (item_id, summary_created_at) VALUES (?, ?)",
                (item["id"], item["summary_at"]),
            )
            report.items += 1
        report.entities_added = resolver.created
    return report


def _edges_for(
    conn: sqlite3.Connection, resolver: _Resolver, item: sqlite3.Row, now: str
) -> list[tuple[int, int, str]]:
    node, created = _item_node(conn, item, now)
    resolver.created += int(created)
    graph = json.loads(item["graph_json"])
    ids: dict[str, int] = {}
    edges: list[tuple[int, int, str]] = []
    for raw in graph.get("entities", []):
        e = ExtractedEntity.model_validate(raw)
        eid = resolver.resolve(e.name, e.type, e.aliases)
        if eid is not None:
            ids[e.name] = eid
            edges.append((node, eid, "mentions"))
    for raw in graph.get("relations", []):
        r = Relation.model_validate(raw)
        if r.src in ids and r.dst in ids and ids[r.src] != ids[r.dst]:
            edges.append((ids[r.src], ids[r.dst], r.relation))
    for author in json.loads(item["authors"])[:MAX_AUTHORS]:
        pid = resolver.resolve(str(author), "Person", [])
        if pid is not None:
            edges.append((node, pid, "authored_by"))
    return edges
