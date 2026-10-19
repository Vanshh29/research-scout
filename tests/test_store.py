import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.models import Item, content_hash
from scout.store.db import connect, count_items, get_item, migrate, snapshot, upsert_item


def make_item(**overrides: object) -> Item:
    fields: dict[str, object] = {
        "id": "arxiv:2401.00001",
        "source": "arxiv",
        "url": "https://arxiv.org/abs/2401.00001",
        "title": "Agents that Judge",
        "authors": ["Ada Lovelace"],
        "published_at": datetime(2024, 1, 1, tzinfo=UTC),
        "fetched_at": datetime(2024, 1, 2, tzinfo=UTC),
        "body": "We study LLM-as-a-judge.",
        "raw_metadata": {"categories": ["cs.CL"]},
    }
    fields.update(overrides)
    return Item.create(**fields)  # type: ignore[arg-type]


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_content_hash_ignores_whitespace_and_case() -> None:
    assert content_hash("Hello  World", "body\n") == content_hash("hello world", " body")
    assert content_hash("a", "b") != content_hash("a", "c")


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_upsert_new_then_unchanged(conn: sqlite3.Connection) -> None:
    item = make_item()
    assert upsert_item(conn, item) == "new"
    assert upsert_item(conn, item) == "unchanged"
    assert count_items(conn) == 1


def test_upsert_detects_changed_content(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    assert upsert_item(conn, make_item(body="Revised abstract (v2).")) == "updated"
    stored = get_item(conn, "arxiv:2401.00001")
    assert stored is not None
    assert stored.body == "Revised abstract (v2)."
    assert count_items(conn) == 1


def test_item_roundtrip(conn: sqlite3.Connection) -> None:
    item = make_item()
    upsert_item(conn, item)
    assert get_item(conn, item.id) == item


def test_snapshot_is_a_standalone_copy(conn: sqlite3.Connection, tmp_path: Path) -> None:
    upsert_item(conn, make_item())
    out = tmp_path / "snap" / "scout.db"
    snapshot(conn, out)
    copy = connect(out)
    assert count_items(copy) == 1
    assert (
        copy.execute("PRAGMA user_version").fetchone()[0]
        == conn.execute("PRAGMA user_version").fetchone()[0]
    )
    snapshot(conn, out)  # overwriting an existing snapshot works
