"""Prompt-injection fixtures: hostile feed content must come out inert and contained."""

import re
from datetime import UTC, datetime
from pathlib import Path

import httpx

from scout.config import RssConfig, RssFeed
from scout.ingest.rss import RssSource
from scout.ingest.run import ingest_sources
from scout.process.sanitize import wrap_document
from scout.store.db import connect, count_items, get_item, migrate

FIXTURE = Path(__file__).parent / "fixtures" / "injection" / "poisoned_feed.xml"
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
CANARY = "CANARY-7f3a91"


def _ingest(tmp_path: Path):  # type: ignore[no-untyped-def]
    xml = FIXTURE.read_text(encoding="utf-8")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=xml)))
    cfg = RssConfig(feeds=[RssFeed(name="poison", url="https://evil.example/feed.xml")])  # type: ignore[arg-type]
    conn = connect(tmp_path / "s.db")
    migrate(conn)
    reports = ingest_sources(conn, [RssSource(cfg, now=lambda: NOW)], client)
    return conn, reports


def test_hostile_item_is_stored_inert(tmp_path: Path) -> None:
    conn, reports = _ingest(tmp_path)
    assert reports[0].status == "ok"
    assert count_items(conn) == 1  # the javascript: link was dropped
    row = conn.execute("SELECT id FROM items").fetchone()
    item = get_item(conn, row["id"])
    assert item is not None
    assert "fetch(" not in item.body  # script content removed
    assert "<" not in item.body and "​" not in item.body and "‮" not in item.body
    assert {"ignore_previous", "fake_role_line", "role_override"} <= set(
        item.raw_metadata["injection_signals"]
    )


def test_canary_stays_inside_one_document_block(tmp_path: Path) -> None:
    conn, _ = _ingest(tmp_path)
    row = conn.execute("SELECT id, body FROM items").fetchone()
    prompt = wrap_document(row["body"], doc_id=row["id"], source="rss")
    assert CANARY in prompt
    assert len(re.findall(r"<document\b", prompt)) == 1
    assert len(re.findall(r"</document\s*>", prompt)) == 1
    inside = prompt.split(">", 1)[1].rsplit("</document>", 1)[0]
    assert CANARY in inside
