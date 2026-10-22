import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.graph.build import build_graph
from scout.graph.queries import clusters, find_entity, neighbors, shortest_path, trending
from scout.store.db import connect, migrate
from tests.test_graph import add_item, ent

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def dated(conn: sqlite3.Connection, n: int, day: str, entities: list[dict[str, object]]) -> None:
    add_item(conn, n, entities, authors=[f"Author {n}"])
    conn.execute(
        "UPDATE items SET published_at = ? WHERE id = ?", (f"{day}T09:00:00+00:00", f"arxiv:{n}")
    )


def test_find_entity_by_name_alias_and_partial(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("RAG", aliases=["Retrieval Augmented Generation"])])
    add_item(conn, 2, [ent("Self-RAG")])
    build_graph(conn)
    assert find_entity(conn, "rag").name == "RAG"  # type: ignore[union-attr]
    assert find_entity(conn, "retrieval-augmented generation").name == "RAG"  # type: ignore[union-attr]
    assert find_entity(conn, "self").name == "Self-RAG"  # type: ignore[union-attr]
    assert find_entity(conn, "nonexistent") is None
    assert find_entity(conn, "!!!") is None


def test_neighbors_by_depth(conn: sqlite3.Connection) -> None:
    add_item(
        conn,
        1,
        [ent("A"), ent("B"), ent("C")],
        [
            {"src": "A", "relation": "builds_on", "dst": "B"},
            {"src": "B", "relation": "implements", "dst": "C"},
        ],
        authors=["One"],
    )
    build_graph(conn)
    # A is linked to its item (mentions) and to B (builds_on); C is reached through B.
    one = {n.node.name: n for n in neighbors(conn, "A", 1)}
    assert set(one) == {"Item 1", "B"}
    assert one["B"].relations == ("builds_on",)
    assert one["Item 1"].relations == ("mentions",)
    assert "One" not in one
    two = {n.node.name: n.distance for n in neighbors(conn, "A", 2)}
    assert two["B"] == 1 and two["One"] == 2
    assert neighbors(conn, "unknown") == []


def test_shortest_path_labels_the_hops(conn: sqlite3.Connection) -> None:
    add_item(
        conn,
        1,
        [ent("A"), ent("B")],
        [{"src": "A", "relation": "implements", "dst": "B"}],
        authors=["One"],
    )
    add_item(conn, 2, [ent("Z")], authors=["Two"])
    build_graph(conn)
    steps = shortest_path(conn, "A", "B")
    assert steps is not None
    assert [n.name for n, _ in steps] == ["A", "B"] and steps[0][1] == "implements"
    assert shortest_path(conn, "A", "Z") is None  # different items, different authors
    assert shortest_path(conn, "A", "missing") is None


def test_path_can_run_through_an_item_node(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("A"), ent("B")])
    build_graph(conn)
    steps = shortest_path(conn, "A", "B")
    assert steps is not None and [n.name for n, _ in steps] == ["A", "Item 1", "B"]


def test_trending_compares_windows_and_flags_new(conn: sqlite3.Connection) -> None:
    for n, day in ((1, "2026-10-20"), (2, "2026-10-21"), (3, "2026-10-19")):
        dated(conn, n, day, [ent("Agents"), ent("Brand new")])
    dated(conn, 4, "2026-10-10", [ent("Agents")])  # previous window
    dated(conn, 5, "2026-10-09", [ent("Agents")])
    dated(conn, 6, "2026-10-21", [ent("Once")])  # below the floor
    build_graph(conn)
    rows = {t.node.name: t for t in trending(conn, now=NOW)}
    assert (rows["Agents"].recent, rows["Agents"].previous, rows["Agents"].is_new) == (3, 2, False)
    assert rows["Brand new"].is_new and rows["Brand new"].recent == 3
    assert "Once" not in rows
    assert not any(t.node.type in {"Person", "Paper"} for t in trending(conn, now=NOW, min_count=1))


def test_trending_empty_database(conn: sqlite3.Connection) -> None:
    assert trending(conn, now=NOW) == []


def test_clusters_group_co_mentioned_concepts(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("A1"), ent("A2"), ent("A3")])
    add_item(conn, 2, [ent("A1"), ent("A2"), ent("A3")])
    add_item(conn, 3, [ent("B1"), ent("B2"), ent("B3")])
    add_item(conn, 4, [ent("B1"), ent("B2"), ent("B3")])
    build_graph(conn)
    found = clusters(conn)
    assert sorted(sorted(c.members) for c in found) == [["A1", "A2", "A3"], ["B1", "B2", "B3"]]
    assert clusters(conn, min_size=4) == []


def test_clusters_with_no_cooccurrence(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("Only")])
    build_graph(conn)
    assert clusters(conn) == []
