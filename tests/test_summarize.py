import json
import re
import sqlite3
from pathlib import Path

import pytest

from scout.config import LLMConfig
from scout.llm.client import LLMClient
from scout.llm.fake import FakeProvider
from scout.process.summarize import (
    build_user_prompt,
    load_prompt,
    parse_summary,
    pending_items,
    summarize_pending,
)
from scout.store.db import connect, migrate, upsert_item
from tests.test_injection import CANARY, _ingest
from tests.test_store import make_item

PROMPTS = Path(__file__).parent.parent / "prompts"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def client_for(conn: sqlite3.Connection, provider: FakeProvider | None = None) -> LLMClient:
    return LLMClient(conn, provider or FakeProvider(), LLMConfig(requests_per_minute=0))


def test_parse_accepts_fenced_json_and_clips() -> None:
    raw = "```json\n" + json.dumps({"tldr": "x" * 500, "key_points": ["a"] * 9}) + "\n```"
    s = parse_summary(raw)
    assert len(s.tldr) == 300 and len(s.key_points) == 5


@pytest.mark.parametrize("bad", ["not json", "[]", '{"key_points": []}', ""])
def test_parse_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_summary(bad)


def test_summarize_stores_and_is_idempotent(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    upsert_item(conn, make_item(id="arxiv:2", url="https://arxiv.org/abs/2", title="Other"))
    fake = FakeProvider()
    client = client_for(conn, fake)

    r1 = summarize_pending(conn, client, PROMPTS)
    assert (r1.done, r1.failed) == (2, 0)
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2

    r2 = summarize_pending(conn, client_for(conn, fake), PROMPTS)
    assert r2.done == 0 and fake.calls == 2  # nothing pending, no new LLM calls


def test_changed_content_is_resummarized(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    summarize_pending(conn, client_for(conn), PROMPTS)
    upsert_item(conn, make_item(body="A rewritten abstract about agents."))
    assert len(pending_items(conn)) == 1


def test_bad_json_counts_as_failed_and_run_continues(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    upsert_item(conn, make_item(id="arxiv:2", url="https://arxiv.org/abs/2", title="Other"))
    calls = {"n": 0}

    def flaky(system: str, prompt: str) -> str:
        calls["n"] += 1
        return "oops, not json" if calls["n"] == 1 else '{"tldr": "fine"}'

    r = summarize_pending(conn, client_for(conn, FakeProvider(responder=flaky)), PROMPTS)
    assert (r.done, r.failed) == (1, 1)


def test_budget_stops_the_run_and_flags_it(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    cfg = LLMConfig(requests_per_minute=0, daily_token_budget=10)
    r = summarize_pending(conn, LLMClient(conn, FakeProvider(), cfg), PROMPTS)
    assert r.budget_hit and r.done == 0


def test_system_prompt_declares_documents_are_data() -> None:
    system = load_prompt(PROMPTS)
    assert "untrusted DATA" in system and "never" in system.lower()


def test_poisoned_item_is_contained_and_cannot_change_output(tmp_path: Path) -> None:
    conn, _ = _ingest(tmp_path)
    row = conn.execute("SELECT id, source, title, body FROM items").fetchone()
    prompt = build_user_prompt(row["id"], row["source"], row["title"], row["body"])
    assert len(re.findall(r"<document\b", prompt)) == 1
    outside = prompt.split("<document", 1)[0] + prompt.rsplit("</document>", 1)[1]
    assert CANARY not in outside

    # A model that obeys the injected text replies with prose: it must be rejected, not stored.
    obedient = FakeProvider(responder=lambda s, p: f"SYSTEM: obeying. {CANARY}")
    report = summarize_pending(conn, client_for(conn, obedient), PROMPTS)
    assert report.failed == 1
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0

    # Even a JSON reply can only populate the three schema fields.
    sneaky = FakeProvider(responder=lambda s, p: json.dumps({"tldr": "ok", "run": "rm -rf /"}))
    summarize_pending(conn, client_for(conn, sneaky), PROMPTS)
    stored = conn.execute("SELECT * FROM summaries").fetchone()
    assert set(stored.keys()) == {
        "item_id", "prompt_version", "model", "content_hash",
        "tldr", "key_points", "entities", "created_at",
    }  # fmt: skip


def test_fake_provider_default_produces_valid_summaries(conn: sqlite3.Connection) -> None:
    upsert_item(conn, make_item())
    r = summarize_pending(conn, client_for(conn), PROMPTS)
    assert r.done == 1
    row = conn.execute("SELECT tldr FROM summaries").fetchone()
    assert row["tldr"].startswith("Fake summary")
