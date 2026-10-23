import sqlite3
from pathlib import Path

import pytest

from scout.store.db import connect, migrate, upsert_item
from scout.store.feedback import UnknownItemError, get_votes, set_vote
from tests.test_store import make_item


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    upsert_item(c, make_item(id="arxiv:1"))
    upsert_item(c, make_item(id="arxiv:2", url="https://arxiv.org/abs/2"))
    return c


def test_vote_is_stored_and_replaced(conn: sqlite3.Connection) -> None:
    assert set_vote(conn, "arxiv:1", 1) == 1
    assert set_vote(conn, "arxiv:1", -1) == -1
    assert get_votes(conn, ["arxiv:1", "arxiv:2"]) == {"arxiv:1": -1}


def test_same_vote_twice_clears_it(conn: sqlite3.Connection) -> None:
    set_vote(conn, "arxiv:1", 1)
    assert set_vote(conn, "arxiv:1", 1) is None
    assert get_votes(conn, ["arxiv:1"]) == {}


def test_unknown_item_and_bad_vote_are_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownItemError):
        set_vote(conn, "arxiv:nope", 1)
    with pytest.raises(ValueError):
        set_vote(conn, "arxiv:1", 0)


def test_get_votes_handles_empty_input(conn: sqlite3.Connection) -> None:
    assert get_votes(conn, []) == {}


def test_migration_reaches_version_10(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10
