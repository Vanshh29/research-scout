from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scout.config import RssConfig, RssFeed
from scout.ingest.http import FetchError
from scout.ingest.rss import RssSource, parse_feed

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def _xml(name: str) -> str:
    return (FIX / f"rss_{name}.xml").read_text(encoding="utf-8")


def test_parse_rss2_feed_with_html_description() -> None:
    items = parse_feed(_xml("lilian-weng"), feed_name="lilian-weng", fetched_at=NOW)
    assert len(items) == 3
    first = items[0]
    assert first.source == "rss"
    assert first.id.startswith("rss:lilian-weng:")
    assert first.title == "Harness Engineering for Self-Improvement"
    assert first.url == "https://lilianweng.github.io/posts/2026-07-04-harness/"
    assert first.published_at == datetime(2026, 7, 4, tzinfo=UTC)
    assert "recursive self-improvement (RSI)" in first.body
    assert "<p>" not in first.body and "&lt;" not in first.body
    assert first.raw_metadata["feed"] == "lilian-weng"


def test_parse_atom_feed_with_author() -> None:
    items = parse_feed(_xml("simon-willison"), feed_name="simon-willison", fetched_at=NOW)
    assert len(items) == 3
    assert items[0].authors == ["Simon Willison"]
    assert items[0].published_at is not None and items[0].published_at.tzinfo is not None
    assert "<strong>" not in items[0].body


def test_entry_without_summary_keeps_title_only() -> None:
    items = parse_feed(_xml("huggingface"), feed_name="huggingface", fetched_at=NOW)
    assert items and all(i.title for i in items)
    assert all(i.url.startswith("https://") for i in items)


def test_content_encoded_is_preferred_and_converted() -> None:
    items = parse_feed(_xml("latent-space"), feed_name="latent-space", fetched_at=NOW)
    assert len(items[0].body) > 1000
    assert not any(tag in items[0].body for tag in ("<p>", "<div", "<a ", "</"))


def test_ids_are_stable_across_parses_and_unique() -> None:
    a = parse_feed(_xml("simon-willison"), feed_name="simon-willison", fetched_at=NOW)
    b = parse_feed(_xml("simon-willison"), feed_name="simon-willison", fetched_at=NOW)
    assert [i.id for i in a] == [i.id for i in b]
    assert len({i.id for i in a}) == 3


def test_non_http_links_are_dropped() -> None:
    xml = """<rss version="2.0"><channel><title>t</title>
      <item><title>bad</title><link>javascript:alert(1)</link><guid>1</guid></item>
      <item><title>good</title><link>https://example.com/g</link><guid>2</guid></item>
    </channel></rss>"""
    items = parse_feed(xml, feed_name="t", fetched_at=NOW)
    assert [i.title for i in items] == ["good"]


def _cfg(**kw: object) -> RssConfig:
    feeds = [
        RssFeed(name="lilian-weng", url="https://a.test/feed.xml"),  # type: ignore[arg-type]
        RssFeed(name="simon-willison", url="https://b.test/feed.xml"),  # type: ignore[arg-type]
    ]
    return RssConfig(feeds=feeds, **kw)  # type: ignore[arg-type]


def _client(responses: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return responses[request.url.host]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok(name: str) -> httpx.Response:
    return httpx.Response(200, text=_xml(name))


def test_fetch_filters_old_entries_and_orders_newest_first() -> None:
    src = RssSource(_cfg(max_age_days=30), now=lambda: NOW)
    items = src.fetch(_client({"a.test": _ok("lilian-weng"), "b.test": _ok("simon-willison")}))
    assert all(i.published_at and (NOW - i.published_at).days <= 30 for i in items)
    assert not any(i.id.startswith("rss:lilian-weng:") for i in items)  # July posts are too old
    dates = [i.published_at for i in items if i.published_at]
    assert dates == sorted(dates, reverse=True)


def test_fetch_caps_items_per_feed() -> None:
    src = RssSource(_cfg(max_age_days=365, max_items_per_feed=1), now=lambda: NOW)
    items = src.fetch(_client({"a.test": _ok("lilian-weng"), "b.test": _ok("simon-willison")}))
    assert len(items) == 2


def test_failing_feed_is_isolated_but_all_failing_raises() -> None:
    src = RssSource(_cfg(), now=lambda: NOW)
    items = src.fetch(_client({"a.test": httpx.Response(404), "b.test": _ok("simon-willison")}))
    assert items
    with pytest.raises(FetchError):
        src.fetch(_client({"a.test": httpx.Response(404), "b.test": httpx.Response(404)}))
