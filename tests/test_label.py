import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from scout.eval.labeler_cli import label_count, next_unlabeled, run_labeling, save_label
from scout.store.db import connect, migrate, upsert_item
from tests.test_store import make_item


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    for n in range(1, 6):
        upsert_item(c, make_item(id=f"arxiv:{n}", url=f"https://arxiv.org/abs/{n}", title=f"T{n}"))
        c.execute(
            "INSERT INTO scores (item_id, content_hash, embed_score, hybrid, weights, scored_at)"
            " VALUES (?, 'h', 0.5, 0.5, '{}', 'now')",
            (f"arxiv:{n}",),
        )
    c.commit()
    return c


def scripted(answers: list[str]) -> Iterator[str]:
    yield from answers


def test_label_saves_relevance_and_skips(conn: sqlite3.Connection) -> None:
    answers = scripted(["2", "", "q"])  # label first, skip second, quit on third
    saved = run_labeling(conn, 5, ask=lambda _: next(answers), say=lambda _: None)
    assert saved == 1 and label_count(conn) == 1
    assert conn.execute("SELECT relevance FROM labels").fetchone()[0] == 2


def test_invalid_input_reprompts(conn: sqlite3.Connection) -> None:
    answers = scripted(["9", "x", "3", "q"])
    run_labeling(conn, 1, ask=lambda _: next(answers), say=lambda _: None)
    assert conn.execute("SELECT relevance FROM labels").fetchone()[0] == 3


def test_summary_ratings_collected_when_summary_exists(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO summaries (item_id, prompt_version, model, content_hash, tldr, key_points,"
        " entities, created_at) VALUES (?, 'v', 'm', 'h', 'a tldr', '[\"p\"]', '[\"E\"]', 'now')",
        (next_unlabeled(conn, 1)[0].item_id,),
    )
    conn.commit()
    answers = scripted(["1", "4", "5", "", "y"])
    run_labeling(conn, 1, ask=lambda _: next(answers), say=lambda _: None)
    row = conn.execute("SELECT * FROM labels").fetchone()
    assert (row["faithfulness"], row["coverage"], row["conciseness"]) == (4, 5, None)
    assert row["entities_ok"] == 1


def test_labeled_items_are_not_offered_again(conn: sqlite3.Connection) -> None:
    first = next_unlabeled(conn, 1)[0]
    save_label(conn, first.item_id, relevance=0)
    assert first.item_id not in {t.item_id for t in next_unlabeled(conn, 5)}


def test_label_order_is_stable_and_not_by_score(conn: sqlite3.Connection) -> None:
    assert next_unlabeled(conn, 5) == next_unlabeled(conn, 5)


def test_db_rejects_out_of_range_relevance(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        save_label(conn, "arxiv:1", relevance=7)
