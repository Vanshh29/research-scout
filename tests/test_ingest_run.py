from datetime import UTC, datetime
from pathlib import Path

import httpx

from scout.config import ArxivConfig
from scout.ingest.arxiv import ArxivSource
from scout.ingest.run import ingest_sources
from scout.models import Item
from scout.store.db import connect, count_items, migrate

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_query.xml"
FETCHED = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)


class ExplodingSource:
    name = "broken"

    def fetch(self, client: httpx.Client) -> list[Item]:
        raise RuntimeError("source is down")


def _client() -> httpx.Client:
    xml = FIXTURE.read_text(encoding="utf-8")
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml)))


def test_ingest_is_idempotent_and_isolates_failures(tmp_path: Path) -> None:
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    arxiv = ArxivSource(ArxivConfig(categories=["cs.AI"], keywords=["agent"]), now=lambda: FETCHED)
    sources = [ExplodingSource(), arxiv]

    first = ingest_sources(conn, sources, _client())
    by_name = {r.source: r for r in first}
    assert by_name["broken"].status == "failed"
    assert "source is down" in (by_name["broken"].error or "")
    assert by_name["arxiv"].status == "ok"
    assert by_name["arxiv"].new == by_name["arxiv"].fetched > 0

    second = ingest_sources(conn, sources, _client())
    arxiv_again = next(r for r in second if r.source == "arxiv")
    assert arxiv_again.new == 0
    assert arxiv_again.unchanged == arxiv_again.fetched
    assert count_items(conn) == by_name["arxiv"].new
