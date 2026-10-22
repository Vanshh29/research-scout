import json
import sqlite3
from pathlib import Path

import pytest

from scout.graph.build import build_graph, load_aliases, normalize
from scout.store.db import connect, migrate, upsert_item
from tests.test_store import make_item


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def add_item(
    conn: sqlite3.Connection,
    n: int,
    entities: list[dict[str, object]],
    relations: list[dict[str, str]] | None = None,
    *,
    authors: list[str] | None = None,
    source: str = "arxiv",
    stamp: str = "2026-10-20",
) -> str:
    item_id = f"{source}:{n}"
    upsert_item(
        conn,
        make_item(
            id=item_id,
            source=source,
            url=f"https://example.org/{n}",
            title=f"Item {n}",
            authors=authors if authors is not None else ["Ada Lovelace"],
        ),
    )
    graph = {"entities": entities, "relations": relations or []}
    conn.execute(
        "INSERT OR REPLACE INTO summaries (item_id, prompt_version, model, content_hash, tldr,"
        " key_points, entities, graph_json, created_at)"
        " SELECT id, 'summarize_v2', 'm', content_hash, 't', '[]', '[]', ?, ? FROM items"
        " WHERE id = ?",
        (json.dumps(graph), stamp, item_id),
    )
    return item_id


def ent(name: str, type_: str = "Concept", aliases: list[str] | None = None) -> dict[str, object]:
    return {"name": name, "type": type_, "aliases": aliases or []}


def names(conn: sqlite3.Connection, type_: str) -> list[str]:
    rows = conn.execute("SELECT name FROM entities WHERE type = ? ORDER BY name", (type_,))
    return [r["name"] for r in rows]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Retrieval-Augmented Generation", "retrieval augmented generation"),
        ("ReAct", "react"),
        ("GPT 4", "GPT-4"),
        ("Self-RAG v2", "Self-RAG"),
        ("Reason+Act", "reason act"),
    ],
)
def test_normalize_treats_these_as_equal(a: str, b: str) -> None:
    assert normalize(a) == normalize(b) != ""


def test_normalize_keeps_different_models_apart() -> None:
    assert normalize("GPT-4") != normalize("GPT-4o")


def test_item_concept_and_author_nodes_with_edges(conn: sqlite3.Connection) -> None:
    add_item(
        conn,
        1,
        [ent("ReAct", "Method"), ent("HotpotQA")],
        [{"src": "ReAct", "relation": "mentions", "dst": "HotpotQA"}],
    )
    report = build_graph(conn)
    assert (report.items, report.entities_added, report.edges_added) == (1, 4, 4)
    assert names(conn, "Paper") == ["Item 1"] and names(conn, "Person") == ["Ada Lovelace"]
    rels = {r["relation"] for r in conn.execute("SELECT relation FROM edges")}
    assert rels == {"mentions", "authored_by"}


def test_github_items_are_repos_and_blogs_are_posts(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [], source="github")
    add_item(conn, 2, [], source="rss")
    build_graph(conn)
    assert names(conn, "Repo") == ["Item 1"] and names(conn, "Post") == ["Item 2"]


def test_rebuild_is_idempotent(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("RAG")])
    build_graph(conn)
    counts = [
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("entities", "edges")
    ]
    again = build_graph(conn)
    assert (again.items, again.skipped) == (0, 1)
    assert counts == [
        conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("entities", "edges")
    ]


def test_same_concept_in_two_items_is_one_node(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("Retrieval-Augmented Generation")])
    add_item(conn, 2, [ent("retrieval augmented generation")])
    build_graph(conn)
    assert names(conn, "Concept") == ["Retrieval-Augmented Generation"]


def test_concept_type_disagreement_does_not_split_the_node(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("AWS Trainium", "Method")])
    add_item(conn, 2, [ent("AWS Trainium", "Concept")])
    build_graph(conn)
    assert names(conn, "Method") == ["AWS Trainium"] and names(conn, "Concept") == []


def test_alias_merges_a_new_name_into_the_known_entity(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("RAG")])
    add_item(conn, 2, [ent("Retrieval Augmented Generation", aliases=["RAG"])])
    build_graph(conn)
    assert names(conn, "Concept") == ["RAG"]
    aliases = conn.execute("SELECT aliases FROM entities WHERE name = 'RAG'").fetchone()[0]
    assert "Retrieval Augmented Generation" in json.loads(aliases)


def test_alias_matching_two_entities_is_ignored_not_merged(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("Agents")])
    add_item(conn, 2, [ent("Tool use")])
    add_item(conn, 3, [ent("LLM agents", aliases=["Agents", "Tool use"])])
    build_graph(conn)
    assert names(conn, "Concept") == ["Agents", "LLM agents", "Tool use"]


def test_seed_aliases_map_to_the_canonical_name(conn: sqlite3.Connection, tmp_path: Path) -> None:
    seeds_file = tmp_path / "aliases.yaml"
    seeds_file.write_text("RAG:\n  - retrieval-augmented generation\n")
    add_item(conn, 1, [ent("Retrieval Augmented Generation")])
    add_item(conn, 2, [ent("RAG")])
    build_graph(conn, load_aliases(seeds_file))
    assert names(conn, "Concept") == ["RAG"]


def test_missing_seed_file_is_fine(tmp_path: Path) -> None:
    assert load_aliases(tmp_path / "nope.yaml") == {}


def test_resummarized_item_replaces_its_edges(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [ent("Old idea")])
    build_graph(conn)
    add_item(conn, 1, [ent("New idea")], stamp="2026-10-22")
    report = build_graph(conn)
    assert report.items == 1
    targets = {
        r["name"]
        for r in conn.execute(
            "SELECT e.name FROM edges g JOIN entities e ON e.id = g.dst WHERE g.relation='mentions'"
        )
    }
    assert targets == {"New idea"}


def test_relations_with_unlisted_ends_and_v1_summaries_are_harmless(
    conn: sqlite3.Connection,
) -> None:
    add_item(conn, 1, [ent("A")], [{"src": "A", "relation": "builds_on", "dst": "Ghost"}])
    upsert_item(conn, make_item(id="arxiv:9", url="https://example.org/9", title="Old"))
    conn.execute(
        "INSERT INTO summaries (item_id, prompt_version, model, content_hash, tldr, key_points,"
        " entities, created_at) SELECT id, 'summarize_v1', 'm', content_hash, 't', '[]', '[]',"
        " '2026-10-19' FROM items WHERE id = 'arxiv:9'"
    )
    report = build_graph(conn)
    assert report.items == 2
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE relation='builds_on'").fetchone()[0] == 0


def test_author_list_is_capped(conn: sqlite3.Connection) -> None:
    add_item(conn, 1, [], authors=[f"Person {i}" for i in range(40)])
    build_graph(conn)
    assert len(names(conn, "Person")) == 10
