"""Read-only graph queries. The NetworkX graph is built on demand from the tables, not pickled."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import networkx as nx
from networkx.algorithms import community

from scout.graph.build import normalize

# Concept-like nodes: not an item node (item_id is set there) and not a person.
_CONCEPT = "e.item_id IS NULL AND e.type != 'Person'"
_MENTIONS = """
    SELECT ed.src AS item_node, ed.dst AS concept, ed.item_id
    FROM edges ed JOIN entities s ON s.id = ed.src
    WHERE ed.relation = 'mentions' AND s.item_id IS NOT NULL"""


@dataclass(frozen=True)
class Node:
    id: int
    name: str
    type: str


@dataclass(frozen=True)
class Neighbor:
    node: Node
    distance: int
    relations: tuple[str, ...]  # edge labels to the start node; only filled at distance 1


@dataclass(frozen=True)
class Trend:
    node: Node
    recent: int
    previous: int

    @property
    def is_new(self) -> bool:
        return self.previous == 0


@dataclass(frozen=True)
class Cluster:
    size: int
    members: tuple[str, ...]  # most connected first


def load_graph(conn: sqlite3.Connection) -> nx.Graph:
    """Undirected view of every node and edge; edge attribute `relations` is a set of labels."""
    g: nx.Graph = nx.Graph()
    for r in conn.execute("SELECT id, name, type FROM entities"):
        g.add_node(r["id"], name=r["name"], type=r["type"])
    for r in conn.execute("SELECT src, dst, relation FROM edges"):
        if g.has_edge(r["src"], r["dst"]):
            g[r["src"]][r["dst"]]["relations"].add(r["relation"])
        else:
            g.add_edge(r["src"], r["dst"], relations={r["relation"]})
    return g


def _node(g: nx.Graph, nid: int) -> Node:
    return Node(nid, g.nodes[nid]["name"], g.nodes[nid]["type"])


def find_entity(conn: sqlite3.Connection, text: str) -> Node | None:
    """Match a concept by name or alias (normalized); fall back to the best partial match."""
    norm = normalize(text)
    if not norm:
        return None
    rows = conn.execute(
        """SELECT e.id, e.name, e.type, e.norm_name, e.aliases,
                  (SELECT COUNT(*) FROM edges g WHERE g.src = e.id OR g.dst = e.id) AS degree
           FROM entities e WHERE e.item_id IS NULL"""
    ).fetchall()
    exact = [r for r in rows if norm == r["norm_name"] or norm in _alias_norms(r["aliases"])]
    partial = [r for r in rows if norm in r["norm_name"]]
    pool = exact or partial
    if not pool:
        return None
    best = max(pool, key=lambda r: (r["degree"], -len(r["norm_name"])))
    return Node(best["id"], best["name"], best["type"])


def _alias_norms(aliases_json: str) -> set[str]:
    return {normalize(a) for a in json.loads(aliases_json)}


def neighbors(conn: sqlite3.Connection, name: str, depth: int = 1) -> list[Neighbor]:
    """Nodes within `depth` hops of an entity, nearest first, most connected first."""
    start = find_entity(conn, name)
    if start is None:
        return []
    g = load_graph(conn)
    dist = nx.single_source_shortest_path_length(g, start.id, cutoff=depth)
    out = [
        Neighbor(
            _node(g, nid),
            d,
            tuple(sorted(g[start.id][nid]["relations"])) if d == 1 else (),
        )
        for nid, d in dist.items()
        if nid != start.id
    ]
    return sorted(out, key=lambda n: (n.distance, -g.degree(n.node.id), n.node.name))


def shortest_path(conn: sqlite3.Connection, a: str, b: str) -> list[tuple[Node, str | None]] | None:
    """Shortest chain between two entities as (node, relations-to-the-next-node) pairs.

    None if either entity is unknown or they are not connected."""
    start, end = find_entity(conn, a), find_entity(conn, b)
    if start is None or end is None:
        return None
    g = load_graph(conn)
    try:
        ids = nx.shortest_path(g, start.id, end.id)
    except nx.NetworkXNoPath:
        return None
    steps: list[tuple[Node, str | None]] = []
    for i, nid in enumerate(ids):
        nxt = ids[i + 1] if i + 1 < len(ids) else None
        label = "/".join(sorted(g[nid][nxt]["relations"])) if nxt is not None else None
        steps.append((_node(g, nid), label))
    return steps


def trending(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    min_count: int = 2,
    limit: int = 10,
    now: datetime | None = None,
) -> list[Trend]:
    """Concepts mentioned by more items in the last `days` than in the `days` before.

    Items are dated by published_at, else first_seen_at. A concept with no mentions in the earlier
    window is flagged new, so a young database degrades to 'new this week' instead of inventing
    growth rates."""
    now = now or datetime.now(UTC)
    mid, start = (
        (now - timedelta(days=days)).isoformat(),
        (now - timedelta(days=2 * days)).isoformat(),
    )
    rows = conn.execute(
        f"""SELECT e.id, e.name, e.type,
                   COUNT(DISTINCT CASE WHEN it.d >= :mid AND it.d <= :now
                                       THEN m.item_id END) AS recent,
                   COUNT(DISTINCT CASE WHEN it.d >= :start AND it.d < :mid
                                       THEN m.item_id END) AS previous
            FROM ({_MENTIONS}) m
            JOIN (SELECT id, COALESCE(published_at, first_seen_at) AS d FROM items) it
                 ON it.id = m.item_id
            JOIN entities e ON e.id = m.concept
            WHERE {_CONCEPT}
            GROUP BY e.id""",
        {"mid": mid, "now": now.isoformat(), "start": start},
    ).fetchall()
    trends = [
        Trend(Node(r["id"], r["name"], r["type"]), r["recent"], r["previous"])
        for r in rows
        if r["recent"] >= min_count
    ]
    trends.sort(key=lambda t: (t.recent - t.previous, t.recent, t.node.name), reverse=True)
    return trends[:limit]


def clusters(conn: sqlite3.Connection, *, min_size: int = 3) -> list[Cluster]:
    """Groups of concepts that tend to be mentioned together (greedy modularity)."""
    g: nx.Graph = nx.Graph()
    for r in conn.execute(
        f"""SELECT a.concept AS a, b.concept AS b, COUNT(DISTINCT a.item_id) AS w
            FROM ({_MENTIONS}) a JOIN ({_MENTIONS}) b
                 ON a.item_id = b.item_id AND a.concept < b.concept
            GROUP BY a.concept, b.concept"""
    ):
        g.add_edge(r["a"], r["b"], weight=r["w"])
    if g.number_of_edges() == 0:
        return []
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM entities")}
    out = []
    for members in community.greedy_modularity_communities(g, weight="weight"):
        if len(members) < min_size:
            continue
        ranked = sorted(members, key=lambda n: (-g.degree(n, weight="weight"), names[n]))
        out.append(Cluster(len(members), tuple(names[n] for n in ranked)))
    return sorted(out, key=lambda c: (-c.size, c.members))
