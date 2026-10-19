import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from scout.config import GithubConfig
from scout.ingest.github import GithubSource, build_query, parse_search
from scout.ingest.http import FetchError

FIXTURE = Path(__file__).parent / "fixtures" / "github_search.json"
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_build_query_uses_topic_stars_and_window() -> None:
    cfg = GithubConfig(topics=["llm-agents"], min_stars=20, created_within_days=30)
    params = build_query("llm-agents", cfg, today=date(2026, 10, 19))
    assert params["q"] == "topic:llm-agents stars:>=20 created:>=2026-09-19"
    assert params["sort"] == "stars"
    assert params["order"] == "desc"


def test_parse_recorded_fixture() -> None:
    items = parse_search(_payload(), fetched_at=NOW)
    assert len(items) == 6
    first = items[0]
    assert first.id == "github:1411033473"
    assert first.source == "github"
    assert first.title == "Gen-Verse/RSI-Anything"
    assert first.url == "https://github.com/Gen-Verse/RSI-Anything"
    assert first.authors == ["Gen-Verse"]
    assert first.published_at == datetime(2026, 10, 8, 22, 52, 16, tzinfo=UTC)
    assert "Recursive Self-Improvement" in first.body
    assert "llm-agents" in first.body
    assert first.raw_metadata["stars"] == 796
    assert first.raw_metadata["stars_per_day"] > 0


def test_star_velocity_is_stars_over_age_in_days() -> None:
    payload = _payload()
    repo = payload["items"][0]
    repo["stargazers_count"] = 100
    repo["created_at"] = "2026-10-09T06:00:00Z"
    (item,) = parse_search({"items": [repo]}, fetched_at=NOW)
    assert item.raw_metadata["stars_per_day"] == pytest.approx(10.0)


def test_brand_new_repo_does_not_divide_by_zero() -> None:
    repo = _payload()["items"][0]
    repo["created_at"] = "2026-10-19T05:59:00Z"
    (item,) = parse_search({"items": [repo]}, fetched_at=NOW)
    assert item.raw_metadata["stars_per_day"] == repo["stargazers_count"]


def test_forks_archived_and_missing_description_are_handled() -> None:
    base = _payload()["items"][0]
    fork = {**base, "id": 1, "fork": True}
    archived = {**base, "id": 2, "archived": True}
    bare = {**base, "id": 3, "description": None, "topics": []}
    items = parse_search({"items": [fork, archived, bare]}, fetched_at=NOW)
    assert [i.id for i in items] == ["github:3"]
    assert items[0].body == ""


def _source(handler: httpx.MockTransport | None = None, **kw: object) -> GithubSource:
    cfg = GithubConfig(topics=["llm-agents", "rag"], min_stars=20, created_within_days=30)
    return GithubSource(cfg, now=lambda: NOW, sleep=lambda _: None, **kw)  # type: ignore[arg-type]


def test_fetch_dedupes_across_topics_and_sorts_by_velocity() -> None:
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    items = _source().fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(items) == 6  # same 6 repos returned for both topics
    velocities = [i.raw_metadata["stars_per_day"] for i in items]
    assert velocities == sorted(velocities, reverse=True)


def test_token_is_sent_only_to_github() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"items": []})

    _source(token="tok").fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen and all(h == "Bearer tok" for h in seen)


def test_one_failing_topic_does_not_lose_the_others() -> None:
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        if "topic:rag" in request.url.params["q"]:
            return httpx.Response(422)
        return httpx.Response(200, json=payload)

    items = _source().fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    assert len(items) == 6


def test_all_topics_failing_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422)

    with pytest.raises(FetchError):
        _source().fetch(httpx.Client(transport=httpx.MockTransport(handler)))
