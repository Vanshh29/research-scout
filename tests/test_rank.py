import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from scout.config import LLMConfig, Profile, Weights
from scout.llm.client import LLMClient
from scout.llm.fake import FakeProvider
from scout.rank.embed_score import embed_items, embedding_score
from scout.rank.embedder import HashEmbedder
from scout.rank.hybrid import hybrid_score, rank_items
from scout.rank.llm_score import Rubric, parse_rubric
from scout.store.db import connect, migrate, upsert_item
from tests.test_store import make_item

PROMPTS = Path(__file__).parent.parent / "prompts"
PROFILE = Profile(
    topics=["LLM agents", "retrieval augmented generation"],
    weights=Weights(embedding=0.4, llm=0.6),
    rubric={"relevance": 0.4, "novelty": 0.2, "evidence_quality": 0.2, "actionability": 0.2},
    top_k=2,
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def add(conn: sqlite3.Connection, n: int, title: str, body: str) -> None:
    upsert_item(
        conn, make_item(id=f"arxiv:{n}", url=f"https://arxiv.org/abs/{n}", title=title, body=body)
    )


def test_hybrid_math() -> None:
    assert hybrid_score(0.5, 1.0, 0.4, 0.6) == pytest.approx(0.8)
    assert hybrid_score(0.5, None, 0.4, 0.6) == 0.5


def test_rubric_weighted_and_clamped() -> None:
    r = parse_rubric(
        json.dumps(
            {
                "relevance": 2,
                "novelty": -1,
                "evidence_quality": 0.5,
                "actionability": 0.5,
                "reason": "x",
            }
        )
    )
    assert (r.relevance, r.novelty) == (1.0, 0.0)
    w = {"relevance": 0.4, "novelty": 0.2, "evidence_quality": 0.2, "actionability": 0.2}
    assert r.weighted(w) == pytest.approx(0.4 + 0.0 + 0.1 + 0.1)


@pytest.mark.parametrize(
    "bad", ["nope", '{"relevance": "high"}', '{"relevance": 1}', '{"relevance": true}']
)
def test_rubric_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_rubric(bad)


def test_rubric_ignores_extra_keys() -> None:
    r = Rubric.model_validate(
        {"relevance": 1, "novelty": 1, "evidence_quality": 1, "actionability": 1, "run": "x"}
    )
    assert not hasattr(r, "run")


def test_embedding_score_prefers_on_topic_and_is_bounded() -> None:
    e = HashEmbedder()
    profile = e.embed(["LLM agents tool use", "retrieval augmented generation"])
    on, off = e.embed(["Building LLM agents with tool use", "Sourdough bread baking tips"])
    s_on = embedding_score(on, profile, e.score_range)
    s_off = embedding_score(off, profile, e.score_range)
    assert 0 <= s_off < s_on <= 1


def test_embeddings_are_cached_and_refreshed_on_change(conn: sqlite3.Connection) -> None:
    add(conn, 1, "Agents", "tool use")
    e = HashEmbedder()
    assert embed_items(conn, e, ["arxiv:1"]) == 1
    assert embed_items(conn, e, ["arxiv:1"]) == 0
    add(conn, 1, "Agents", "now different body text")
    assert embed_items(conn, e, ["arxiv:1"]) == 1


def test_vectors_are_normalized_float32(conn: sqlite3.Connection) -> None:
    add(conn, 1, "Agents", "tool use")
    embed_items(conn, HashEmbedder(), ["arxiv:1"])
    blob = conn.execute("SELECT vector FROM embeddings").fetchone()[0]
    v = np.frombuffer(blob, dtype=np.float32)
    assert v.shape == (384,) and np.linalg.norm(v) == pytest.approx(1.0, abs=1e-5)


def make_world(conn: sqlite3.Connection) -> None:
    add(conn, 1, "LLM agents with tool use", "agents call tools; retrieval augmented generation")
    add(conn, 2, "Retrieval augmented generation for agents", "RAG with LLM agents")
    add(conn, 3, "Sourdough baking", "flour water salt")
    add(conn, 4, "Gardening notes", "tomatoes and soil")


def client_for(conn: sqlite3.Connection, **cfg: object) -> LLMClient:
    return LLMClient(conn, FakeProvider(), LLMConfig(requests_per_minute=0, **cfg))  # type: ignore[arg-type]


def test_rank_scores_topk_with_llm_and_rest_embedding_only(conn: sqlite3.Connection) -> None:
    make_world(conn)
    r = rank_items(conn, HashEmbedder(), client_for(conn), PROFILE, PROMPTS)
    assert (r.candidates, r.llm_scored) == (4, 2)
    rows = {x["item_id"]: x for x in conn.execute("SELECT * FROM scores")}
    scored = {i for i, x in rows.items() if x["llm_score"] is not None}
    assert scored == {"arxiv:1", "arxiv:2"}
    assert rows["arxiv:3"]["hybrid"] == rows["arxiv:3"]["embed_score"]
    top = rows["arxiv:1"]
    assert top["hybrid"] == pytest.approx(0.4 * top["embed_score"] + 0.6 * top["llm_score"])
    assert set(json.loads(top["llm_breakdown"])) == set(PROFILE.rubric)


def test_rank_is_idempotent_and_cached(conn: sqlite3.Connection) -> None:
    make_world(conn)
    client = client_for(conn)
    rank_items(conn, HashEmbedder(), client, PROFILE, PROMPTS)
    client2 = client_for(conn)
    r = rank_items(conn, HashEmbedder(), client2, PROFILE, PROMPTS)
    assert client2.calls == 0 and r.embedded == 0
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 4


def test_rank_without_llm_degrades_to_embedding_only(conn: sqlite3.Connection) -> None:
    make_world(conn)
    r = rank_items(conn, HashEmbedder(), None, PROFILE, PROMPTS)
    assert r.llm_scored == 0
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE llm_score IS NULL").fetchone()[0] == 4


def test_rank_budget_hit_is_flagged_and_keeps_embedding_scores(conn: sqlite3.Connection) -> None:
    make_world(conn)
    r = rank_items(conn, HashEmbedder(), client_for(conn, daily_token_budget=10), PROFILE, PROMPTS)
    assert r.budget_hit and r.llm_scored == 0
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 4


def test_rank_only_considers_recent_items(conn: sqlite3.Connection) -> None:
    make_world(conn)
    future = datetime.now(UTC) + timedelta(days=30)
    r = rank_items(conn, HashEmbedder(), None, PROFILE, PROMPTS, now=future)
    assert r.candidates == 0
