"""SQLite connection, migrations, and item persistence."""

import json
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Literal

from scout.models import Item

UpsertResult = Literal["new", "updated", "unchanged"]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migration_files() -> list[tuple[int, str]]:
    folder = resources.files("scout.store") / "migrations"
    found = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            version = int(entry.name.split("_", 1)[0])
            found.append((version, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, tracked via PRAGMA user_version. Returns the new version."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in _migration_files():
        if version > current:
            with conn:
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {version}")
            current = version
    return current


def _now() -> str:
    return datetime.now(UTC).isoformat()


def upsert_item(conn: sqlite3.Connection, item: Item) -> UpsertResult:
    """Insert a new item, update it if its content changed, or leave it alone."""
    row = conn.execute("SELECT content_hash FROM items WHERE id = ?", (item.id,)).fetchone()
    if row is not None and row["content_hash"] == item.content_hash:
        return "unchanged"

    values = {
        "id": item.id,
        "source": item.source,
        "url": item.url,
        "title": item.title,
        "authors": json.dumps(item.authors),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "fetched_at": item.fetched_at.isoformat(),
        "body": item.body,
        "content_hash": item.content_hash,
        "raw_metadata": json.dumps(item.raw_metadata),
        "now": _now(),
    }
    with conn:
        if row is None:
            conn.execute(
                """INSERT INTO items (id, source, url, title, authors, published_at, fetched_at,
                       body, content_hash, raw_metadata, first_seen_at, updated_at)
                   VALUES (:id, :source, :url, :title, :authors, :published_at, :fetched_at,
                       :body, :content_hash, :raw_metadata, :now, :now)""",
                values,
            )
            return "new"
        conn.execute(
            """UPDATE items SET url = :url, title = :title, authors = :authors,
                   published_at = :published_at, fetched_at = :fetched_at, body = :body,
                   content_hash = :content_hash, raw_metadata = :raw_metadata, updated_at = :now
               WHERE id = :id""",
            values,
        )
        return "updated"


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source=row["source"],
        url=row["url"],
        title=row["title"],
        authors=json.loads(row["authors"]),
        published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        body=row["body"],
        content_hash=row["content_hash"],
        raw_metadata=json.loads(row["raw_metadata"]),
    )


def get_item(conn: sqlite3.Connection, item_id: str) -> Item | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def count_items(conn: sqlite3.Connection, source: str | None = None) -> int:
    if source is None:
        result: int = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    else:
        result = conn.execute("SELECT COUNT(*) FROM items WHERE source = ?", (source,)).fetchone()[
            0
        ]
    return result


def snapshot(conn: sqlite3.Connection, out: Path) -> None:
    """Write a compact, self-contained copy of the database (no WAL sidecar files)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    conn.execute("VACUUM INTO ?", (str(out),))
