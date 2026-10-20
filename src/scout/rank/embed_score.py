"""Stage 1 of ranking: embed items and score them against the interest profile (free, local)."""

import sqlite3

import numpy as np

from scout.config import Profile
from scout.rank.embedder import Embedder, Vector


def item_text(title: str, body: str) -> str:
    return f"{title}\n{body[:1500]}"


def profile_texts(profile: Profile) -> list[str]:
    return [*profile.topics, *profile.seed_papers, *profile.seed_repos]


def embed_items(conn: sqlite3.Connection, embedder: Embedder, item_ids: list[str]) -> int:
    """Embed items lacking a vector for this model and content. Returns how many were embedded."""
    todo = []
    for item_id in item_ids:
        row = conn.execute(
            """SELECT i.id, i.title, i.body, i.content_hash, e.content_hash AS done
               FROM items i LEFT JOIN embeddings e ON e.item_id = i.id AND e.model = ?
               WHERE i.id = ?""",
            (embedder.name, item_id),
        ).fetchone()
        if row is not None and row["done"] != row["content_hash"]:
            todo.append(row)
    if not todo:
        return 0
    vectors = embedder.embed([item_text(r["title"], r["body"]) for r in todo])
    with conn:
        for row, vec in zip(todo, vectors, strict=True):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (item_id, model, content_hash, vector)"
                " VALUES (?, ?, ?, ?)",
                (row["id"], embedder.name, row["content_hash"], vec.tobytes()),
            )
    return len(todo)


def load_vector(conn: sqlite3.Connection, item_id: str, model: str) -> Vector | None:
    row = conn.execute(
        "SELECT vector FROM embeddings WHERE item_id = ? AND model = ?", (item_id, model)
    ).fetchone()
    return None if row is None else np.frombuffer(row["vector"], dtype=np.float32)


def embedding_score(
    vec: Vector, profile_vecs: list[Vector], score_range: tuple[float, float], *, k: int = 3
) -> float:
    """Mean of the k highest cosines to profile entries, mapped linearly to 0..1.

    Using the nearest few entries (not the centroid) lets an item match one of several topics."""
    sims = np.sort(np.stack(profile_vecs) @ vec)[-k:]
    low, high = score_range
    return float(np.clip((float(sims.mean()) - low) / (high - low), 0.0, 1.0))
