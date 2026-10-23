import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scout.api.app import create_app
from scout.config import Settings
from scout.rank.embed_score import embed_items
from scout.rank.embedder import Embedder, HashEmbedder
from scout.search import Searcher, keyword_search
from scout.store.db import connect, migrate, upsert_item
from tests.test_store import make_item

TEXTS = {
    "1": ("RAG evaluation", "retrieval augmented generation evaluation faithfulness benchmark"),
    "2": ("Judging RAG", "retrieval augmented generation judge evaluation metrics"),
    "3": ("Agent tools", "language model agents call tools functions planning"),
    "4": ("GPU kernels", "cuda kernels memory bandwidth throughput"),
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    c = connect(path)
    migrate(c)
    for n, (title, body) in TEXTS.items():
        upsert_item(
            c,
            make_item(id=f"arxiv:{n}", url=f"https://arxiv.org/abs/{n}", title=title, body=body),
        )
    embed_items(c, HashEmbedder(), [f"arxiv:{n}" for n in TEXTS])
    c.close()
    return path


def settings_for(db_path: Path, **env: object) -> Settings:
    base: dict[str, object] = {"SCOUT_LLM_PROVIDER": "fake"}  # fake provider => HashEmbedder
    return Settings(_env_file=None, SCOUT_DB_PATH=str(db_path), **(base | env))  # type: ignore[call-arg]


def client_for(db_path: Path, searcher: Searcher | None = None, **env: object) -> TestClient:
    return TestClient(create_app(settings_for(db_path, **env), searcher=searcher))


def broken_embedder() -> Embedder:
    raise RuntimeError("model could not be loaded")


def test_semantic_search_ranks_by_meaning(db_path: Path) -> None:
    conn = connect(db_path)
    result = Searcher(settings_for(db_path)).search(
        conn, "evaluating retrieval augmented generation", 3
    )
    conn.close()
    assert result.mode == "semantic"
    assert {h.item_id for h in result.hits[:2]} == {"arxiv:1", "arxiv:2"}
    assert result.hits[0].score is not None and result.hits[0].title


def test_similar_excludes_the_item_itself(db_path: Path) -> None:
    conn = connect(db_path)
    hits = Searcher(settings_for(db_path)).similar(conn, "arxiv:1", 2)
    conn.close()
    assert hits[0].item_id == "arxiv:2" and "arxiv:1" not in {h.item_id for h in hits}


def test_index_picks_up_items_embedded_after_it_was_built(db_path: Path) -> None:
    searcher = Searcher(settings_for(db_path))
    conn = connect(db_path)
    assert "arxiv:9" not in {h.item_id for h in searcher.search(conn, "quantum", 5).hits}
    upsert_item(conn, make_item(id="arxiv:9", url="https://arxiv.org/abs/9", title="Quantum"))
    embed_items(conn, HashEmbedder(), ["arxiv:9"])
    assert "arxiv:9" in {h.item_id for h in searcher.search(conn, "quantum", 5).hits}
    conn.close()


def test_falls_back_to_keywords_when_the_embedder_cannot_load(db_path: Path) -> None:
    searcher = Searcher(settings_for(db_path), embedder_factory=broken_embedder)
    conn = connect(db_path)
    result = searcher.search(conn, "cuda", 5)
    conn.close()
    assert result.mode == "keyword" and [h.item_id for h in result.hits] == ["arxiv:4"]


def test_keyword_search_escapes_like_wildcards(db_path: Path) -> None:
    conn = connect(db_path)
    assert keyword_search(conn, "%", 5) == []
    assert keyword_search(conn, "_", 5) == []
    assert [h.item_id for h in keyword_search(conn, "agents", 5)] == ["arxiv:3"]
    conn.close()


def test_search_page_shows_results_and_nav(db_path: Path) -> None:
    page = client_for(db_path).get("/search", params={"q": "cuda kernels"})
    assert page.status_code == 200 and "GPU kernels" in page.text
    assert 'href="/search"' in page.text and "/similar/arxiv%3A4" in page.text


def test_search_page_without_query_shows_only_the_form(db_path: Path) -> None:
    page = client_for(db_path).get("/search")
    assert page.status_code == 200 and "<input" in page.text and "GPU kernels" not in page.text


def test_search_page_validates_parameters(db_path: Path) -> None:
    c = client_for(db_path)
    assert c.get("/search", params={"q": "x" * 301}).status_code == 422
    assert c.get("/search", params={"q": "x", "k": 99}).status_code == 422
    assert c.get("/search", params={"q": "x", "k": 0}).status_code == 422


def test_search_page_says_when_it_fell_back_to_keywords(db_path: Path) -> None:
    searcher = Searcher(settings_for(db_path), embedder_factory=broken_embedder)
    page = client_for(db_path, searcher=searcher).get("/search", params={"q": "cuda"})
    assert page.status_code == 200 and "keyword" in page.text.lower() and "GPU kernels" in page.text


def test_search_works_in_demo_mode(db_path: Path) -> None:
    page = client_for(db_path, SCOUT_DEMO="1").get("/search", params={"q": "cuda"})
    assert page.status_code == 200 and "GPU kernels" in page.text


def test_similar_page_and_unknown_item(db_path: Path) -> None:
    c = client_for(db_path)
    ok = c.get("/similar/arxiv:1")
    assert ok.status_code == 200 and "Judging RAG" in ok.text
    assert c.get("/similar/arxiv:nope").status_code == 404


def test_hostile_titles_and_urls_stay_inert(db_path: Path) -> None:
    conn: sqlite3.Connection = connect(db_path)
    upsert_item(
        conn,
        make_item(
            id="arxiv:evil",
            url="javascript:alert(1)",
            title="<script>alert('x')</script> cuda",
            body="cuda kernels",
        ),
    )
    embed_items(conn, HashEmbedder(), ["arxiv:evil"])
    conn.close()
    page = (
        client_for(db_path).get("/search", params={"q": "<img src=x onerror=alert(1)> cuda"}).text
    )
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "<img src=x" not in page and 'href="javascript:' not in page
