"""GitHub source: recently created repos per topic, ranked by star velocity."""

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from scout.config import GithubConfig
from scout.ingest.http import FetchError, get_with_retry
from scout.log import get_logger
from scout.models import Item

log = get_logger("github")

SEARCH_URL = "https://api.github.com/search/repositories"
_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def build_query(topic: str, cfg: GithubConfig, *, today: date) -> dict[str, Any]:
    since = today - timedelta(days=cfg.created_within_days)
    return {
        "q": f"topic:{topic} stars:>={cfg.min_stars} created:>={since.isoformat()}",
        "sort": "stars",
        "order": "desc",
        "per_page": 50,
    }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_search(payload: dict[str, Any], *, fetched_at: datetime) -> list[Item]:
    """Parse a search response into Items. Forks and archived repos are skipped."""
    items = []
    for repo in payload.get("items", []):
        if repo.get("fork") or repo.get("archived"):
            continue
        created = _parse_time(repo["created_at"])
        age_days = max((fetched_at - created).total_seconds() / 86400, 1.0)
        stars = repo.get("stargazers_count", 0)
        topics = repo.get("topics") or []
        description = (repo.get("description") or "").strip()
        body = f"{description}\n\nTopics: {', '.join(topics)}" if topics else description
        items.append(
            Item.create(
                id=f"github:{repo['id']}",
                source="github",
                url=repo["html_url"],
                title=repo["full_name"],
                authors=[repo["owner"]["login"]],
                published_at=created,
                fetched_at=fetched_at,
                body=body,
                raw_metadata={
                    "stars": stars,
                    "forks": repo.get("forks_count", 0),
                    "stars_per_day": stars / age_days,
                    "language": repo.get("language"),
                    "topics": topics,
                    "pushed_at": repo.get("pushed_at"),
                    "license": (repo.get("license") or {}).get("spdx_id"),
                },
            )
        )
    return items


class GithubSource:
    name = "github"

    def __init__(
        self,
        cfg: GithubConfig,
        *,
        token: str | None = None,
        max_retries: int = 4,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.token = token
        self.max_retries = max_retries
        self.now = now
        self.sleep = sleep

    def _headers(self) -> dict[str, str]:
        # Sent per request so the token never reaches other hosts via the shared client.
        headers = dict(_HEADERS)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def fetch(self, client: httpx.Client) -> list[Item]:
        fetched_at = self.now()
        by_id: dict[str, Item] = {}
        errors: list[str] = []
        for n, topic in enumerate(self.cfg.topics):
            if n:
                self.sleep(2.0)  # search API allows ~10 req/min unauthenticated
            try:
                resp = get_with_retry(
                    client,
                    SEARCH_URL,
                    params=build_query(topic, self.cfg, today=fetched_at.date()),
                    headers=self._headers(),
                    max_retries=self.max_retries,
                )
            except FetchError as exc:
                log.warning("topic failed", extra={"topic": topic, "error": str(exc)})
                errors.append(str(exc))
                continue
            for item in parse_search(resp.json(), fetched_at=fetched_at):
                by_id.setdefault(item.id, item)
        if errors and len(errors) == len(self.cfg.topics):
            raise FetchError("; ".join(errors))
        return sorted(by_id.values(), key=lambda i: -i.raw_metadata["stars_per_day"])
