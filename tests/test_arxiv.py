from datetime import UTC, datetime
from pathlib import Path

import httpx

from scout.config import ArxivConfig
from scout.ingest.arxiv import ArxivSource, build_query, matches_keywords, parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_query.xml"
FETCHED = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

SMALL_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/cs/0101001v3</id>
    <title>An Old-Style
      Identifier   Paper</title>
    <summary>  Tool use for
      agents. </summary>
    <published>2001-01-01T00:00:00Z</published>
    <author><name>A. Turing</name></author>
    <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
    <arxiv:primary_category term="cs.AI"/>
  </entry>
</feed>"""


def test_parse_recorded_fixture() -> None:
    items = parse_feed(FIXTURE.read_text(encoding="utf-8"), fetched_at=FETCHED)
    assert len(items) == 25
    first = items[0]
    assert first.id == "arxiv:2610.22372"
    assert first.url == "https://arxiv.org/abs/2610.22372"
    assert first.source == "arxiv"
    assert first.title.startswith("LIBERO-PeRM")
    assert "Zhixu Li" in first.authors
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert "cs.AI" in first.raw_metadata["categories"]
    assert first.raw_metadata["version"] == 1
    assert len({i.id for i in items}) == 25


def test_parse_normalizes_old_style_ids_and_whitespace() -> None:
    (item,) = parse_feed(SMALL_FEED, fetched_at=FETCHED)
    assert item.id == "arxiv:cs/0101001"
    assert item.raw_metadata["version"] == 3
    assert item.title == "An Old-Style Identifier Paper"
    assert item.body == "Tool use for agents."


def test_keyword_match_respects_word_boundaries() -> None:
    kws = ["RAG", "agent", "tool use"]
    assert matches_keywords("A RAG pipeline", kws)
    assert matches_keywords("Multi-agent systems", kws)
    assert matches_keywords("LLM agents that plan", kws)
    assert matches_keywords("Learning Tool Use", kws)
    assert not matches_keywords("Cold storage on average", kws)
    assert not matches_keywords("Reagents in chemistry", kws)


def test_build_query_ors_categories() -> None:
    params = build_query(ArxivConfig(categories=["cs.AI", "cs.CL"], keywords=[], max_results=50))
    assert params["search_query"] == "cat:cs.AI OR cat:cs.CL"
    assert params["sortBy"] == "submittedDate"
    assert params["max_results"] == 50


def test_source_fetch_filters_by_keywords() -> None:
    xml = FIXTURE.read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, text=xml)

    cfg = ArxivConfig(categories=["cs.AI"], keywords=["benchmark"], max_results=25)
    source = ArxivSource(cfg, now=lambda: FETCHED)
    items = source.fetch(httpx.Client(transport=httpx.MockTransport(handler)))
    assert items
    assert all(matches_keywords(f"{i.title} {i.body}", ["benchmark"]) for i in items)
    assert len(items) < 25
