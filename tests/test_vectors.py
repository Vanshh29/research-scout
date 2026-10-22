import sqlite3
from pathlib import Path

import pytest

from scout.rank.embed_score import embed_items
from scout.rank.embedder import HashEmbedder
from scout.store.db import connect, migrate, upsert_item
from scout.store.vectors import NumpyIndex, Vec0Index, VecUnavailable, open_index, similar
from tests.test_store import make_item

TEXTS = {
    "1": ("RAG evaluation", "retrieval augmented generation evaluation faithfulness benchmark"),
    "2": ("Judging RAG", "retrieval augmented generation judge evaluation metrics"),
    "3": ("Agent tools", "language model agents call tools functions planning"),
    "4": ("GPU kernels", "cuda kernels memory bandwidth throughput"),
}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    for n, (title, body) in TEXTS.items():
        upsert_item(
            c,
            make_item(id=f"arxiv:{n}", url=f"https://arxiv.org/abs/{n}", title=title, body=body),
        )
    embed_items(c, HashEmbedder(), [f"arxiv:{n}" for n in TEXTS])
    return c


def vec0_or_fail(path: Path) -> Vec0Index:
    """sqlite-vec must load wherever tests run; a silent skip would hide a broken CI image."""
    try:
        return Vec0Index(path, HashEmbedder.name, 384)
    except VecUnavailable as exc:
        pytest.fail(f"sqlite-vec failed to load: {exc}")


def ids(hits: list) -> list[str]:  # type: ignore[type-arg]
    return [h.item_id for h in hits]


def test_backends_agree_on_top_k(conn: sqlite3.Connection, tmp_path: Path) -> None:
    emb = HashEmbedder()
    q = emb.embed(["evaluating retrieval augmented generation"])[0]
    np_index = NumpyIndex(emb.name)
    np_index.sync(conn)
    v_index = vec0_or_fail(tmp_path / "v.db")
    v_index.sync(conn)
    assert ids(np_index.search(q, 3)) == ids(v_index.search(q, 3))
    assert set(ids(np_index.search(q, 2))) == {"arxiv:1", "arxiv:2"}
    assert v_index.search(q, 1)[0].score == pytest.approx(np_index.search(q, 1)[0].score, abs=1e-4)


def test_vec0_sync_is_incremental(conn: sqlite3.Connection, tmp_path: Path) -> None:
    index = vec0_or_fail(tmp_path / "v.db")
    assert index.sync(conn) == 4
    assert index.sync(conn) == 0
    conn.execute("DELETE FROM embeddings WHERE item_id = 'arxiv:4'")
    index.sync(conn)
    q = HashEmbedder().embed(["cuda kernels"])[0]
    assert "arxiv:4" not in ids(index.search(q, 4))


def test_similar_excludes_the_item_itself(conn: sqlite3.Connection) -> None:
    index = NumpyIndex(HashEmbedder.name)
    index.sync(conn)
    hits = similar(conn, index, "arxiv:1", HashEmbedder.name, 2)
    assert ids(hits)[0] == "arxiv:2"
    assert "arxiv:1" not in ids(hits)
    assert similar(conn, index, "arxiv:missing", HashEmbedder.name, 2) == []


def test_empty_index_returns_nothing(tmp_path: Path) -> None:
    c = connect(tmp_path / "e.db")
    migrate(c)
    index = open_index(c, HashEmbedder.name, tmp_path / "v.db", backend="numpy")
    assert index.search(HashEmbedder().embed(["x"])[0], 3) == []


def test_auto_falls_back_to_numpy(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: sqlite3.Connection) -> None:
        raise VecUnavailable("no extension support")

    monkeypatch.setattr("scout.store.vectors.load_sqlite_vec", boom)
    index = open_index(conn, HashEmbedder.name, tmp_path / "v.db")
    assert index.backend == "numpy"
    with pytest.raises(VecUnavailable):
        open_index(conn, HashEmbedder.name, tmp_path / "v.db", backend="vec0")
