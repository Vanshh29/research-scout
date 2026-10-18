"""Core data model shared by every pipeline stage."""

import hashlib
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

SourceName = Literal["arxiv", "github", "rss"]


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def content_hash(title: str, body: str) -> str:
    """Stable hash of an item's content; whitespace/case changes don't count as edits."""
    payload = f"{_normalize(title)}\n{_normalize(body)}".encode()
    return hashlib.sha256(payload).hexdigest()


class Item(BaseModel):
    id: str
    source: SourceName
    url: str
    title: str
    authors: list[str]
    published_at: datetime | None
    fetched_at: datetime
    body: str
    content_hash: str
    raw_metadata: dict[str, Any]

    @classmethod
    def create(
        cls,
        *,
        id: str,
        source: SourceName,
        url: str,
        title: str,
        authors: list[str],
        published_at: datetime | None,
        fetched_at: datetime,
        body: str,
        raw_metadata: dict[str, Any] | None = None,
    ) -> "Item":
        """Build an Item, computing `content_hash` from title + body."""
        return cls(
            id=id,
            source=source,
            url=url,
            title=title,
            authors=authors,
            published_at=published_at,
            fetched_at=fetched_at,
            body=body,
            content_hash=content_hash(title, body),
            raw_metadata=raw_metadata or {},
        )
