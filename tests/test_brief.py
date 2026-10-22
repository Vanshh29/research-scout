import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from scout.brief import Brief, BriefEntry, build_brief, record_brief, render_telegram, render_text
from scout.config import LLMConfig, Profile, Weights
from scout.deliver.telegram import DeliveryError, send_message
from scout.deliver.webhook import post_brief
from scout.llm.client import LLMClient
from scout.llm.fake import FakeProvider
from scout.rank.embedder import HashEmbedder
from scout.rank.hybrid import rank_items
from scout.scheduler.run_daily import Pipeline, run_daily
from scout.store.db import connect, migrate, upsert_item
from scout.store.runs import last_run
from tests.test_store import make_item

PROMPTS = Path(__file__).parent.parent / "prompts"
PROFILE = Profile(
    topics=["LLM agents", "retrieval augmented generation"],
    weights=Weights(embedding=0.4, llm=0.6),
    rubric={"relevance": 0.4, "novelty": 0.2, "evidence_quality": 0.2, "actionability": 0.2},
    top_k=3,
    top_n=2,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def seed(conn: sqlite3.Connection) -> None:
    for n, (title, body) in enumerate(
        [
            ("LLM agents with tool use", "agents call tools"),
            ("Retrieval augmented generation", "RAG for agents"),
            ("Sourdough baking", "flour and water"),
        ],
        start=1,
    ):
        upsert_item(
            conn,
            make_item(id=f"arxiv:{n}", url=f"https://arxiv.org/abs/{n}", title=title, body=body),
        )


def ranked(conn: sqlite3.Connection) -> None:
    seed(conn)
    client = LLMClient(conn, FakeProvider(), LLMConfig(requests_per_minute=0))
    rank_items(conn, HashEmbedder(), client, PROFILE, PROMPTS)


def test_brief_picks_top_n_by_hybrid_with_breakdown(conn: sqlite3.Connection) -> None:
    ranked(conn)
    b = build_brief(conn, top_n=2)
    assert [e.rank for e in b.entries] == [1, 2]
    assert b.entries[0].hybrid >= b.entries[1].hybrid
    assert "Sourdough baking" not in [e.title for e in b.entries]
    assert set(b.entries[0].breakdown) == set(PROFILE.rubric)
    assert b.new_items == 3


def test_recorded_items_are_not_shown_again(conn: sqlite3.Connection) -> None:
    ranked(conn)
    first = build_brief(conn, top_n=2)
    record_brief(conn, first, run_id=None)
    second = build_brief(conn, top_n=2)
    assert {e.item_id for e in first.entries}.isdisjoint(e.item_id for e in second.entries)
    assert any(
        "No new ranked items" in w for w in build_brief(conn, top_n=2, since_days=0).warnings
    )


def test_unrecorded_preview_does_not_consume(conn: sqlite3.Connection) -> None:
    ranked(conn)
    assert build_brief(conn, top_n=2).entries == build_brief(conn, top_n=2).entries


def test_telegram_render_escapes_untrusted_text_and_links() -> None:
    evil = BriefEntry(
        rank=1,
        item_id="x",
        title="<script>alert(1)</script> & co",
        url="javascript:alert(1)",
        source="rss",
        hybrid=0.5,
        embed_score=0.5,
        llm_score=None,
        tldr="<b>bold</b>",
        reason='"><a href="evil">',
    )
    text = render_telegram(Brief(date="2026-10-20", new_items=1, entries=[evil]))
    assert "<script>" not in text and "<b>bold</b>" not in text
    assert "javascript:" not in text and "&lt;script&gt;" in text
    assert 'href="evil"' not in text


def test_telegram_render_stays_under_limit() -> None:
    entries = [
        BriefEntry(
            rank=n,
            item_id=str(n),
            title="T" * 200,
            url="https://x.org",
            source="arxiv",
            hybrid=0.5,
            embed_score=0.5,
            llm_score=0.5,
            tldr="s" * 300,
            reason="r" * 200,
        )
        for n in range(1, 30)
    ]
    text = render_telegram(Brief(date="d", new_items=29, entries=entries))
    assert len(text) < 4096 and "more on the dashboard" in text


def test_text_render_includes_scores_and_warnings() -> None:
    b = Brief(date="d", new_items=0, entries=[], warnings=["budget hit"])
    assert "budget hit" in render_text(b)


def test_send_message_posts_and_hides_token_on_error() -> None:
    seen: dict[str, object] = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    send_message(
        httpx.Client(transport=httpx.MockTransport(ok)), token="T0KEN", chat_id="42", text="hi"
    )
    assert seen["chat_id"] == "42" and seen["parse_mode"] == "HTML"

    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    with pytest.raises(DeliveryError) as err:
        send_message(bad, token="T0KEN", chat_id="42", text="hi")
    assert "T0KEN" not in str(err.value)


def test_send_message_retries_server_errors() -> None:
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502) if attempts["n"] < 3 else httpx.Response(200)

    send_message(
        httpx.Client(transport=httpx.MockTransport(flaky)),
        token="t", chat_id="1", text="x", sleep=lambda _: None,
    )  # fmt: skip
    assert attempts["n"] == 3


def test_webhook_noop_when_unset_and_posts_when_set() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    b = Brief(date="d", new_items=0, entries=[])
    assert post_brief(client, None, b) is False
    assert post_brief(client, "https://n8n.example/hook", b) is True
    with pytest.raises(DeliveryError):
        post_brief(client, "file:///etc/passwd", b)


def pipeline(conn: sqlite3.Connection, **kw: object) -> Pipeline:
    llm = LLMClient(conn, FakeProvider(), LLMConfig(requests_per_minute=0))
    return Pipeline(
        embedder=HashEmbedder(),
        profile=PROFILE,
        prompts_dir=PROMPTS,
        llm=llm,
        **kw,  # type: ignore[arg-type]
    )


def test_run_daily_end_to_end_records_tokens_and_delivers(conn: sqlite3.Connection) -> None:
    seed(conn)
    sent: list[dict[str, object]] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(telegram))
    p = pipeline(conn, telegram_token="t", telegram_chat_id="9")
    summary = run_daily(conn, [], client, pipeline=p)
    assert summary.brief is not None and len(summary.brief.entries) == 2
    assert len(sent) == 1 and sent[0]["chat_id"] == "9"
    run = last_run(conn)
    assert run is not None and run["tokens"] > 0
    assert conn.execute("SELECT COUNT(*) FROM brief_items").fetchone()[0] == 2


def test_failed_delivery_keeps_items_for_retry(conn: sqlite3.Connection) -> None:
    seed(conn)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    summary = run_daily(
        conn, [], client, pipeline=pipeline(conn, telegram_token="t", telegram_chat_id="9")
    )
    assert any("telegram" in w for w in summary.warnings)
    assert conn.execute("SELECT COUNT(*) FROM brief_items").fetchone()[0] == 0


def test_missing_llm_degrades_and_brief_says_so(conn: sqlite3.Connection) -> None:
    seed(conn)
    p = Pipeline(
        embedder=HashEmbedder(), profile=PROFILE, prompts_dir=PROMPTS, llm=None,
        llm_note="LLM unavailable (GEMINI_API_KEY is not set)",
    )  # fmt: skip
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    summary = run_daily(conn, [], client, pipeline=p)
    assert summary.brief is not None and summary.brief.entries
    assert any("GEMINI_API_KEY" in w for w in summary.brief.warnings)
    assert any("embedding similarity only" in w for w in summary.brief.warnings)


def test_stage_crash_does_not_stop_the_brief(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(conn)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("model exploded")

    monkeypatch.setattr("scout.scheduler.run_daily.summarize_pending", boom)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    summary = run_daily(conn, [], client, pipeline=pipeline(conn))
    assert summary.brief is not None and summary.brief.entries
    assert any("summarize stage failed" in w for w in summary.brief.warnings)


def test_llm_scored_items_outrank_embedding_only_items(conn: sqlite3.Connection) -> None:
    seed(conn)
    for item_id, embed, llm, hybrid in [
        ("arxiv:1", 0.5, 0.4, 0.44),  # LLM-scored, lower blended value
        ("arxiv:2", 0.9, None, 0.9),  # embedding only, higher raw value
    ]:
        conn.execute(
            "INSERT INTO scores (item_id, content_hash, embed_score, llm_score, hybrid, weights,"
            " scored_at) VALUES (?, 'h', ?, ?, ?, '{}', 'now')",
            (item_id, embed, llm, hybrid),
        )
    conn.commit()
    assert [e.item_id for e in build_brief(conn, top_n=2).entries] == ["arxiv:1", "arxiv:2"]
