"""arXiv source: newest submissions in the configured categories, filtered by keywords."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from typing import Any

import feedparser
import httpx

from scout.config import ArxivConfig
from scout.ingest.http import get_with_retry
from scout.models import Item

API_URL = "https://export.arxiv.org/api/query"
_ID_RE = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<version>\d+))?$")


def _clean(text: str) -> str:
    return " ".join(text.split())


@cache
def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    # Word-bounded, optional plural: "agent" matches "agents" but not "reagents".
    return re.compile(rf"(?<![\w]){re.escape(keyword)}s?(?![\w])", re.IGNORECASE)


def matches_keywords(text: str, keywords: list[str]) -> bool:
    return any(_keyword_pattern(k).search(text) for k in keywords)


def build_query(cfg: ArxivConfig) -> dict[str, Any]:
    return {
        "search_query": " OR ".join(f"cat:{c}" for c in cfg.categories),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": cfg.max_results,
    }


def _parse_time(struct: Any) -> datetime | None:
    if not struct:
        return None
    year, month, day, hour, minute, second = struct[:6]
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def parse_feed(xml: str, *, fetched_at: datetime) -> list[Item]:
    """Parse an arXiv Atom response into Items. Pure: no network, no clock."""
    items = []
    for entry in feedparser.parse(xml).entries:
        match = _ID_RE.search(entry.get("id", ""))
        if not match:
            continue
        arxiv_id = match.group("id")
        pdf_url = next(
            (
                link["href"]
                for link in entry.get("links", [])
                if link.get("type") == "application/pdf"
            ),
            None,
        )
        items.append(
            Item.create(
                id=f"arxiv:{arxiv_id}",
                source="arxiv",
                url=f"https://arxiv.org/abs/{arxiv_id}",
                title=_clean(entry.get("title", "")),
                authors=[a["name"] for a in entry.get("authors", []) if a.get("name")],
                published_at=_parse_time(entry.get("published_parsed")),
                fetched_at=fetched_at,
                body=_clean(entry.get("summary", "")),
                raw_metadata={
                    "categories": [t["term"] for t in entry.get("tags", [])],
                    "primary_category": entry.get("arxiv_primary_category", {}).get("term"),
                    "version": int(match.group("version") or 1),
                    "pdf_url": pdf_url,
                    "comment": entry.get("arxiv_comment"),
                },
            )
        )
    return items


class ArxivSource:
    name = "arxiv"

    def __init__(
        self,
        cfg: ArxivConfig,
        *,
        max_retries: int = 4,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.max_retries = max_retries
        self.now = now

    def fetch(self, client: httpx.Client) -> list[Item]:
        resp = get_with_retry(
            client, API_URL, params=build_query(self.cfg), max_retries=self.max_retries
        )
        items = parse_feed(resp.text, fetched_at=self.now())
        return [i for i in items if matches_keywords(f"{i.title} {i.body}", self.cfg.keywords)]
