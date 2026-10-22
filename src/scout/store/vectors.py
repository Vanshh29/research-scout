"""Vector search over the stored embeddings: sqlite-vec when it loads, numpy otherwise.

The `embeddings` table stays the source of truth. The sqlite-vec index is a derived cache in its
own file, so a machine where the extension cannot load never breaks migrations or the snapshot.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from scout.log import get_logger
from scout.rank.embedder import Vector

log = get_logger("vectors")

Backend = Literal["auto", "vec0", "numpy"]


class VecUnavailable(RuntimeError):
    """The sqlite-vec extension could not be loaded in this Python/SQLite build."""


@dataclass(frozen=True)
class Hit:
    item_id: str
    score: float  # cosine similarity, higher is closer


class VectorIndex(Protocol):
    backend: str

    def sync(self, conn: sqlite3.Connection) -> int:
        """Bring the index in line with `embeddings`. Returns rows added or changed."""
        ...

    def search(self, vec: Vector, k: int) -> list[Hit]: ...


def load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the extension into `conn` or raise VecUnavailable."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, ImportError, sqlite3.OperationalError) as exc:
        raise VecUnavailable(str(exc)) from exc


def _stored(conn: sqlite3.Connection, model: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT item_id, content_hash, vector FROM embeddings WHERE model = ? ORDER BY item_id",
        (model,),
    ).fetchall()


class NumpyIndex:
    """Brute-force cosine over the whole matrix. Fine below ~50k items (see ADR-006)."""

    backend = "numpy"

    def __init__(self, model: str) -> None:
        self._model = model
        self._ids: list[str] = []
        self._matrix = np.zeros((0, 0), dtype=np.float32)

    def sync(self, conn: sqlite3.Connection) -> int:
        rows = _stored(conn, self._model)
        self._ids = [r["item_id"] for r in rows]
        if rows:
            self._matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
        else:
            self._matrix = np.zeros((0, 0), dtype=np.float32)
        return len(rows)

    def search(self, vec: Vector, k: int) -> list[Hit]:
        if not self._ids:
            return []
        sims = self._matrix @ vec
        top = np.argsort(-sims, kind="stable")[:k]
        return [Hit(self._ids[i], float(sims[i])) for i in top]


class Vec0Index:
    """sqlite-vec `vec0` table in a separate, rebuildable database file."""

    backend = "vec0"

    def __init__(self, path: Path, model: str, dim: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._model = model
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        load_sqlite_vec(self._db)
        self._db.executescript(
            f"""CREATE TABLE IF NOT EXISTS indexed (
                    item_id TEXT PRIMARY KEY, model TEXT NOT NULL, content_hash TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS item_vectors USING vec0(
                    item_id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine);"""
        )

    def sync(self, conn: sqlite3.Connection) -> int:
        rows = _stored(conn, self._model)
        done = {r["item_id"]: r["content_hash"] for r in self._db.execute("SELECT * FROM indexed")}
        wanted = {r["item_id"] for r in rows}
        changed = 0
        with self._db:
            for item_id in done.keys() - wanted:
                self._db.execute("DELETE FROM item_vectors WHERE item_id = ?", (item_id,))
                self._db.execute("DELETE FROM indexed WHERE item_id = ?", (item_id,))
            for r in rows:
                if done.get(r["item_id"]) == r["content_hash"]:
                    continue
                self._db.execute("DELETE FROM item_vectors WHERE item_id = ?", (r["item_id"],))
                self._db.execute(
                    "INSERT INTO item_vectors (item_id, embedding) VALUES (?, ?)",
                    (r["item_id"], r["vector"]),
                )
                self._db.execute(
                    "INSERT OR REPLACE INTO indexed (item_id, model, content_hash)"
                    " VALUES (?, ?, ?)",
                    (r["item_id"], self._model, r["content_hash"]),
                )
                changed += 1
        return changed

    def search(self, vec: Vector, k: int) -> list[Hit]:
        rows = self._db.execute(
            "SELECT item_id, distance FROM item_vectors WHERE embedding MATCH ? AND k = ?"
            " ORDER BY distance",
            (vec.tobytes(), k),
        ).fetchall()
        return [Hit(r["item_id"], 1.0 - float(r["distance"])) for r in rows]


def open_index(
    conn: sqlite3.Connection,
    model: str,
    index_path: Path,
    *,
    backend: Backend = "auto",
    dim: int = 384,
) -> VectorIndex:
    """Open an index and sync it. `auto` tries sqlite-vec and falls back to numpy."""
    index: VectorIndex
    if backend != "numpy":
        try:
            index = Vec0Index(index_path, model, dim)
        except VecUnavailable:
            if backend == "vec0":
                raise
            log.warning("sqlite-vec unavailable, using numpy search")
            index = NumpyIndex(model)
    else:
        index = NumpyIndex(model)
    index.sync(conn)
    return index


def similar(
    conn: sqlite3.Connection, index: VectorIndex, item_id: str, model: str, k: int
) -> list[Hit]:
    """Nearest neighbours of an item, excluding the item itself."""
    row = conn.execute(
        "SELECT vector FROM embeddings WHERE item_id = ? AND model = ?", (item_id, model)
    ).fetchone()
    if row is None:
        return []
    vec = np.frombuffer(row["vector"], dtype=np.float32)
    return [h for h in index.search(vec, k + 1) if h.item_id != item_id][:k]
