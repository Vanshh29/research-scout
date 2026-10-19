"""RSS / Atom source: recent posts from the configured blogs."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import httpx

from scout.config import RssConfig
from scout.ingest.http import FetchError, get_with_retry
from scout.log import get_logger
from scout.models import Item
from scout.process.sanitize import html_to_text, is_http_url

log = get_logger("rss")


def _parse_time(struct: Any) -> datetime | None:
    if not struct:
        return None
    year, month, day, hour, minute, second = struct[:6]
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _entry_body(entry: Any) -> str:
    # Full content beats the summary when a feed ships both.
    content = entry.get("content")
    raw = content[0]["value"] if content else entry.get("summary", "")
    return html_to_text(raw)


def parse_feed(xml: str | bytes, *, feed_name: str, fetched_at: datetime) -> list[Item]:
    """Parse an RSS or Atom document into Items. Pure: no network, no clock."""
    parsed = feedparser.parse(xml)
    feed_author = parsed.feed.get("author")
    items = []
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not is_http_url(url):
            continue
        native_id = entry.get("id") or url
        digest = hashlib.sha256(native_id.encode()).hexdigest()[:16]
        author = entry.get("author") or feed_author
        items.append(
            Item.create(
                id=f"rss:{feed_name}:{digest}",
                source="rss",
                url=url,
                title=" ".join(entry.get("title", "").split()),
                authors=[author] if author else [],
                published_at=_parse_time(entry.get("published_parsed"))
                or _parse_time(entry.get("updated_parsed")),
                fetched_at=fetched_at,
                body=_entry_body(entry),
                raw_metadata={
                    "feed": feed_name,
                    "tags": [t["term"] for t in entry.get("tags", []) if t.get("term")],
                },
            )
        )
    return items


class RssSource:
    name = "rss"

    def __init__(
        self,
        cfg: RssConfig,
        *,
        max_retries: int = 4,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.max_retries = max_retries
        self.now = now

    def fetch(self, client: httpx.Client) -> list[Item]:
        fetched_at = self.now()
        cutoff = fetched_at - timedelta(days=self.cfg.max_age_days)
        items: list[Item] = []
        errors: list[str] = []
        for feed in self.cfg.feeds:
            try:
                resp = get_with_retry(client, str(feed.url), max_retries=self.max_retries)
            except FetchError as exc:
                log.warning("feed failed", extra={"feed": feed.name, "error": str(exc)})
                errors.append(str(exc))
                continue
            fresh = [
                i
                for i in parse_feed(resp.content, feed_name=feed.name, fetched_at=fetched_at)
                if i.published_at is None or i.published_at >= cutoff
            ]
            fresh.sort(key=lambda i: i.published_at or fetched_at, reverse=True)
            items.extend(fresh[: self.cfg.max_items_per_feed])
        if errors and len(errors) == len(self.cfg.feeds):
            raise FetchError("; ".join(errors))
        return sorted(items, key=lambda i: i.published_at or fetched_at, reverse=True)
