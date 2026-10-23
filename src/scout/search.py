"""Search for the dashboard: semantic when the local embedder loads, keywords when it does not.

Embedding the query is the only heavy step, so the model loads lazily on the first search and is
kept. The index is an in-memory numpy matrix (no file written, safe for a read-only deploy) that is
rebuilt whenever the number of stored embeddings changes."""

import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from scout.config import Settings
from scout.log import get_logger
from scout.rank.embedder import Embedder, make_embedder
from scout.store.vectors import Hit, NumpyIndex, similar

log = get_logger("search")

SearchMode = Literal["semantic", "keyword"]


@dataclass(frozen=True)
class SearchHit:
    item_id: str
    title: str
    url: str
    source: str
    tldr: str | None
    score: float | None  # cosine similarity; None for keyword matches


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    mode: SearchMode


def _hydrate(conn: sqlite3.Connection, hits: Sequence[Hit]) -> list[SearchHit]:
    out = []
    for h in hits:
        row = conn.execute(
            """SELECT i.id, i.title, i.url, i.source,
                      (SELECT tldr FROM latest_summaries s WHERE s.item_id = i.id) AS tldr
               FROM items i WHERE i.id = ?""",
            (h.item_id,),
        ).fetchone()
        if row is not None:
            out.append(
                SearchHit(row["id"], row["title"], row["url"], row["source"], row["tldr"], h.score)
            )
    return out


def _like_escape(word: str) -> str:
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def keyword_search(conn: sqlite3.Connection, query: str, k: int) -> list[SearchHit]:
    """Items whose title or body contains every word of the query; title matches first."""
    words = [w for w in query.split() if w][:8]
    if not words:
        return []
    clause = " AND ".join(
        "(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\')" for _ in words
    )
    params: list[object] = []
    for w in words:
        pattern = f"%{_like_escape(w)}%"
        params += [pattern, pattern]
    title_first = " AND ".join("i.title LIKE ? ESCAPE '\\'" for _ in words)
    params += [f"%{_like_escape(w)}%" for w in words]
    rows = conn.execute(
        f"""SELECT i.id, i.title, i.url, i.source,
                   (SELECT tldr FROM latest_summaries s WHERE s.item_id = i.id) AS tldr
            FROM items i WHERE {clause}
            ORDER BY ({title_first}) DESC, COALESCE(i.published_at, i.first_seen_at) DESC, i.id
            LIMIT ?""",
        [*params, k],
    ).fetchall()
    return [SearchHit(r["id"], r["title"], r["url"], r["source"], r["tldr"], None) for r in rows]


class Searcher:
    def __init__(
        self, settings: Settings, *, embedder_factory: Callable[[], Embedder] | None = None
    ) -> None:
        self._factory = embedder_factory or (
            lambda: make_embedder(fake=settings.llm_provider == "fake")
        )
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None
        self._index: NumpyIndex | None = None
        self._synced = -1
        self._failed = False

    def _ready(self, conn: sqlite3.Connection) -> tuple[Embedder, NumpyIndex] | None:
        """The embedder and an index in step with the database, or None if the model won't load."""
        if self._failed:
            return None
        if self._embedder is None:
            try:
                self._embedder = self._factory()
            except Exception:  # model files missing, no memory, offline first download
                log.warning("embedder unavailable, using keyword search", exc_info=True)
                self._failed = True
                return None
            self._index = NumpyIndex(self._embedder.name)
        assert self._index is not None
        count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE model = ?", (self._embedder.name,)
        ).fetchone()[0]
        if count != self._synced:
            self._index.sync(conn)
            self._synced = count
        return self._embedder, self._index

    def search(self, conn: sqlite3.Connection, query: str, k: int) -> SearchResult:
        with self._lock:
            ready = self._ready(conn)
            if ready is not None:
                embedder, index = ready
                hits = index.search(embedder.embed([query])[0], k)
                return SearchResult(_hydrate(conn, hits), "semantic")
        return SearchResult(keyword_search(conn, query, k), "keyword")

    def similar(self, conn: sqlite3.Connection, item_id: str, k: int) -> list[SearchHit]:
        with self._lock:
            ready = self._ready(conn)
            if ready is None:
                return []
            embedder, index = ready
            return _hydrate(conn, similar(conn, index, item_id, embedder.name, k))
