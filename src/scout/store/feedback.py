"""Thumbs up/down votes from the dashboard. A repeat vote of the same kind clears the row."""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime


class UnknownItemError(LookupError):
    pass


def set_vote(
    conn: sqlite3.Connection, item_id: str, vote: int, *, now: datetime | None = None
) -> int | None:
    """Store the vote and return the vote now in force (None when a repeat vote cleared it)."""
    if vote not in (-1, 1):
        raise ValueError("vote must be -1 or 1")
    if conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone() is None:
        raise UnknownItemError(item_id)
    with conn:
        current = conn.execute("SELECT vote FROM feedback WHERE item_id = ?", (item_id,)).fetchone()
        if current is not None and current["vote"] == vote:
            conn.execute("DELETE FROM feedback WHERE item_id = ?", (item_id,))
            return None
        conn.execute(
            "INSERT OR REPLACE INTO feedback (item_id, vote, created_at) VALUES (?, ?, ?)",
            (item_id, vote, (now or datetime.now(UTC)).isoformat()),
        )
    return vote


def get_votes(conn: sqlite3.Connection, item_ids: Sequence[str]) -> dict[str, int]:
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT item_id, vote FROM feedback WHERE item_id IN ({marks})", list(item_ids)
    ).fetchall()
    return {r["item_id"]: r["vote"] for r in rows}
