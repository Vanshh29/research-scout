import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scout.api import views
from scout.api.app import ago, create_app, duration, safe_url, when
from scout.brief import build_brief, record_brief
from scout.config import Settings
from scout.graph.build import build_graph
from scout.ingest.run import SourceReport
from scout.store.db import connect, migrate, upsert_item
from scout.store.runs import finish_run, start_run
from tests.test_brief import ranked
from tests.test_graph import add_item, ent
from tests.test_store import make_item


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "t.db"
    conn = connect(path)
    migrate(conn)
    conn.close()
    return path


def client_for(db_path: Path, **env: object) -> TestClient:
    settings = Settings(_env_file=None, SCOUT_DB_PATH=str(db_path), **env)  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def seed_brief(db_path: Path) -> None:
    conn = connect(db_path)
    ranked(conn)
    record_brief(conn, build_brief(conn, top_n=2), run_id=None)
    conn.close()


def seed_run(db_path: Path, *, failed: bool = False) -> None:
    conn = connect(db_path)
    run_id = start_run(conn, trigger="cron")
    reports = [
        SourceReport(source="arxiv", status="ok", fetched=10, new=4, latency_ms=900),
        SourceReport(
            source="github",
            status="failed" if failed else "ok",
            fetched=0 if failed else 5,
            new=0 if failed else 2,
            latency_ms=300,
            error="HTTP 403" if failed else None,
        ),
    ]
    finish_run(conn, run_id, reports, latency_ms=5200, tokens=12345, cost_usd=0.0123)
    conn.close()


def test_empty_database_shows_helpful_empty_states(db_path: Path) -> None:
    c = client_for(db_path)
    home = c.get("/")
    assert home.status_code == 200 and "scout run-daily" in home.text
    runs = c.get("/runs")
    assert runs.status_code == 200 and "No runs recorded yet" in runs.text


def test_healthz_reports_schema_version(db_path: Path) -> None:
    body = client_for(db_path).get("/healthz").json()
    assert body["status"] == "ok" and body["schema"] >= 9 and body["demo"] is False


def test_brief_page_lists_ranked_items_with_breakdown(db_path: Path) -> None:
    seed_brief(db_path)
    page = client_for(db_path).get("/").text
    assert "Today's brief" in page and "LLM agents with tool use" in page
    assert "Score details" in page and "Relevance" in page
    assert "live preview" not in page


def test_unrecorded_brief_is_marked_as_preview(db_path: Path) -> None:
    conn = connect(db_path)
    ranked(conn)
    conn.close()
    page = client_for(db_path).get("/").text
    assert "live preview" in page


def test_latest_brief_with_items_wins_over_an_empty_one(db_path: Path) -> None:
    seed_brief(db_path)
    conn = connect(db_path)
    record_brief(conn, build_brief(conn, top_n=2, since_days=0), run_id=None)  # empty brief
    page = views.todays_brief(conn)
    conn.close()
    assert page.entries and not page.is_preview


def test_hostile_content_is_rendered_inert(db_path: Path) -> None:
    conn = connect(db_path)
    upsert_item(
        conn,
        make_item(
            id="arxiv:evil",
            url="javascript:alert(1)",
            title="<script>alert('x')</script> agents <img src=x onerror=alert(1)>",
            body="LLM agents with tool use",
        ),
    )
    ranked(conn)
    record_brief(conn, build_brief(conn, top_n=8), run_id=None)
    conn.close()
    page = client_for(db_path).get("/").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "onerror=alert" not in page.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert 'href="javascript:' not in page


def test_security_headers_are_set(db_path: Path) -> None:
    resp = client_for(db_path).get("/")
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "<script" not in resp.text and ' style="' not in resp.text  # nothing the CSP would block


def test_runs_page_shows_sources_cost_and_errors(db_path: Path) -> None:
    seed_run(db_path, failed=True)
    page = client_for(db_path).get("/runs").text
    assert "arxiv" in page and "github" in page and "HTTP 403" in page
    assert "12,345" in page and "$0.0123" in page and "partial" in page


def test_status_chip_in_header_after_a_run(db_path: Path) -> None:
    seed_run(db_path)
    assert "Last run ok" in client_for(db_path).get("/").text


def test_demo_mode_is_announced(db_path: Path) -> None:
    assert "Read-only demo" in client_for(db_path, SCOUT_DEMO="1").get("/").text


def test_old_snapshot_is_upgraded_on_start(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = connect(path)
    migrate(conn)
    conn.execute("DROP VIEW latest_summaries")
    conn.execute("PRAGMA user_version = 7")
    conn.close()
    # migrations 008 to 010 are not idempotent; an older snapshot lacks their tables entirely
    conn = connect(path)
    for table in ("feedback", "graph_built", "edges", "entities"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("ALTER TABLE summaries DROP COLUMN graph_json")
    conn.close()
    assert client_for(path).get("/healthz").json()["schema"] >= 9


def test_helpers() -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    assert ago((now - timedelta(hours=3)).isoformat(), now) == "3 hours ago"
    assert ago((now - timedelta(days=1)).isoformat(), now) == "1 day ago"
    assert ago(None) == "never" and ago("garbage") == "never"
    assert when("2026-10-22T09:05:00+00:00") == "22 Oct, 09:05 UTC"
    assert when(None) == ""
    assert duration(900) == "0.9 s" and duration(120_000) == "2.0 min"
    assert safe_url("https://x.org/a") == "https://x.org/a"
    assert safe_url("javascript:alert(1)") == "" and safe_url("data:text/html,x") == ""


def test_views_work_on_a_plain_connection(db_path: Path) -> None:
    conn: sqlite3.Connection = connect(db_path)
    assert views.runs_page(conn).last is None
    assert views.todays_brief(conn).is_preview
    conn.close()


def seed_two_runs(db_path: Path, *, now: datetime, github_now: str = "failed") -> None:
    """An older run where both sources were ok, then a newer run where github is `github_now`."""
    conn = connect(db_path)
    for when_run, github in ((now - timedelta(days=1), "ok"), (now, github_now)):
        run_id = start_run(conn, trigger="cron", now=when_run)
        reports = [
            SourceReport(source="arxiv", status="ok", fetched=3, new=1, latency_ms=100),
            SourceReport(source="github", status=github, fetched=0, new=0, latency_ms=100),
        ]
        finish_run(conn, run_id, reports, latency_ms=500, now=when_run)
    conn.close()


def seed_changed(db_path: Path, *, now: datetime) -> None:
    """One item seen before the latest run and a ranked one seen after it started."""
    conn = connect(db_path)
    ranked(conn)
    old = (now - timedelta(days=2)).isoformat()
    conn.execute("UPDATE items SET first_seen_at = ?", (old,))
    conn.execute("UPDATE items SET first_seen_at = ? WHERE id = 'arxiv:1'", (now.isoformat(),))
    conn.execute(
        "INSERT INTO entities (type, name, norm_name, first_seen_at) VALUES (?, ?, ?, ?)",
        ("Concept", "Tool use", "tool use", now.isoformat()),
    )
    conn.execute(
        "INSERT INTO entities (type, name, norm_name, first_seen_at) VALUES (?, ?, ?, ?)",
        ("Concept", "Old idea", "old idea", old),
    )
    conn.commit()
    conn.close()


def test_changed_view_lists_only_what_is_new_since_the_last_run(db_path: Path) -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    seed_two_runs(db_path, now=now)
    seed_changed(db_path, now=now + timedelta(minutes=5))
    conn = connect(db_path)
    page = views.changed_today(conn, now=now + timedelta(hours=1))
    conn.close()
    assert [i.item_id for i in page.new_items] == ["arxiv:1"] and page.total_new == 1
    assert page.new_concepts == ["Tool use"]
    assert [(c.source, c.before, c.after) for c in page.source_changes] == [
        ("github", "ok", "failed")
    ]


def test_changed_view_without_runs_is_empty(db_path: Path) -> None:
    conn = connect(db_path)
    page = views.changed_today(conn)
    conn.close()
    assert page.since is None and not page.new_items and not page.source_changes


def test_changed_page_quiet_day_and_no_source_change(db_path: Path) -> None:
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    seed_two_runs(db_path, now=now, github_now="ok")
    page = client_for(db_path).get("/changed")
    assert page.status_code == 200
    assert "Quiet day" in page.text and "Source health" not in page.text


def test_changed_page_renders_changes_and_nav(db_path: Path) -> None:
    now = datetime.now(UTC)
    seed_two_runs(db_path, now=now)
    seed_changed(db_path, now=now + timedelta(minutes=1))
    resp = client_for(db_path).get("/changed")
    assert resp.status_code == 200
    assert "LLM agents with tool use" in resp.text and "Tool use" in resp.text
    assert "Old idea" not in resp.text and "github" in resp.text
    assert 'href="/changed"' in resp.text


def test_changed_page_escapes_hostile_titles(db_path: Path) -> None:
    now = datetime.now(UTC)
    seed_two_runs(db_path, now=now)
    conn = connect(db_path)
    upsert_item(
        conn,
        make_item(
            id="arxiv:evil",
            url="javascript:alert(1)",
            title="<script>alert('x')</script> agents",
            body="LLM agents with tool use",
        ),
    )
    ranked(conn)
    conn.execute("UPDATE items SET first_seen_at = ?", ((now + timedelta(minutes=1)).isoformat(),))
    conn.commit()
    conn.close()
    page = client_for(db_path).get("/changed").text
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def seed_graph(db_path: Path, *, name: str = "Tool use") -> None:
    conn = connect(db_path)
    add_item(
        conn,
        1,
        [ent(name), ent("Planning")],
        [{"src": name, "relation": "builds_on", "dst": "Planning"}],
        authors=["One"],
    )
    build_graph(conn)
    conn.close()


def test_graph_page_serves_vendored_script_without_inline_code(db_path: Path) -> None:
    resp = client_for(db_path).get("/graph")
    assert resp.status_code == 200 and 'href="/graph"' in resp.text
    assert '<script src="/static/vendor/cytoscape-3.30.2.min.js"' in resp.text
    assert '<script src="/static/graph.js"' in resp.text
    assert "<script>" not in resp.text and ' style="' not in resp.text
    assert client_for(db_path).get("/static/vendor/cytoscape-3.30.2.min.js").status_code == 200


def test_graph_api_returns_nodes_edges_and_items(db_path: Path) -> None:
    seed_graph(db_path)
    body = client_for(db_path).get("/api/graph", params={"center": "tool use"}).json()
    assert body["center"] == "Tool use" and body["truncated"] is False
    names = {n["name"] for n in body["nodes"]}
    assert {"Tool use", "Planning"} <= names
    ids = {n["id"] for n in body["nodes"]}
    assert body["edges"] and all(e["src"] in ids and e["dst"] in ids for e in body["edges"])
    assert [i["title"] for i in body["items"]] == ["Item 1"]


def test_graph_api_overview_unknown_center_and_empty_db(db_path: Path) -> None:
    empty = client_for(db_path).get("/api/graph").json()
    assert empty == {"center": None, "nodes": [], "edges": [], "items": [], "truncated": False}
    seed_graph(db_path)
    c = client_for(db_path)
    assert c.get("/api/graph").json()["nodes"]
    unknown = c.get("/api/graph", params={"center": "zzz"})
    assert unknown.status_code == 200 and unknown.json()["nodes"] == []


def test_graph_api_validates_parameters(db_path: Path) -> None:
    c = client_for(db_path)
    assert c.get("/api/graph", params={"depth": 9}).status_code == 422
    assert c.get("/api/graph", params={"center": "x" * 500}).status_code == 422


def test_graph_api_hostile_names_and_urls_stay_data(db_path: Path) -> None:
    seed_graph(db_path, name="<img src=x onerror=alert(1)>")
    conn = connect(db_path)
    conn.execute("UPDATE items SET url = 'javascript:alert(1)'")
    conn.commit()
    conn.close()
    resp = client_for(db_path).get("/api/graph", params={"center": "onerror"})
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["items"][0]["url"] == ""
    assert "javascript:" not in resp.text


TOKEN = "s3cret-token-value"
VOTE_HEADERS = {"X-Scout": "1"}


def writable_client(db_path: Path) -> TestClient:
    seed_brief(db_path)
    return client_for(db_path, SCOUT_API_TOKEN=TOKEN)


def unlocked_client(db_path: Path) -> TestClient:
    c = writable_client(db_path)
    resp = c.post("/unlock", content=f"token={TOKEN}", headers=_FORM, follow_redirects=False)
    assert resp.status_code == 303
    return c


_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def test_votes_are_refused_in_demo_even_with_a_valid_token(db_path: Path) -> None:
    seed_brief(db_path)
    c = client_for(db_path, SCOUT_API_TOKEN=TOKEN, SCOUT_DEMO="1")
    body = {"item_id": "arxiv:1", "vote": 1}
    assert c.post("/api/feedback", json=body, headers=BEARER).status_code == 403
    assert c.post("/unlock", content=f"token={TOKEN}", headers=_FORM).status_code == 403


def test_votes_are_refused_when_no_token_is_configured(db_path: Path) -> None:
    seed_brief(db_path)
    c = client_for(db_path)
    resp = c.post("/api/feedback", json={"item_id": "arxiv:1", "vote": 1}, headers=BEARER)
    assert resp.status_code == 403
    assert c.post("/unlock", content="token=x", headers=_FORM).status_code == 403


def test_missing_or_wrong_credentials_are_401(db_path: Path) -> None:
    c = writable_client(db_path)
    body = {"item_id": "arxiv:1", "vote": 1}
    assert c.post("/api/feedback", json=body).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert c.post("/api/feedback", json=body, headers=bad).status_code == 401
    assert c.post("/unlock", content="token=nope", headers=_FORM).status_code == 401
    assert c.post("/unlock", content="", headers=_FORM).status_code == 401


def test_bearer_token_can_vote_and_toggle(db_path: Path) -> None:
    c = writable_client(db_path)
    body = {"item_id": "arxiv:1", "vote": 1}
    assert c.post("/api/feedback", json=body, headers=BEARER).json() == {
        "item_id": "arxiv:1",
        "vote": 1,
    }
    assert c.post("/api/feedback", json=body, headers=BEARER).json()["vote"] is None


def test_cookie_auth_needs_the_csrf_header(db_path: Path) -> None:
    c = unlocked_client(db_path)
    body = {"item_id": "arxiv:1", "vote": -1}
    assert c.post("/api/feedback", json=body).status_code == 403
    ok = c.post("/api/feedback", json=body, headers=VOTE_HEADERS)
    assert ok.status_code == 200 and ok.json()["vote"] == -1


def test_cookie_is_httponly_strict_and_does_not_hold_the_token(db_path: Path) -> None:
    c = writable_client(db_path)
    resp = c.post("/unlock", content=f"token={TOKEN}", headers=_FORM, follow_redirects=False)
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and TOKEN not in cookie


def test_vote_validation(db_path: Path) -> None:
    c = writable_client(db_path)
    send = lambda body: c.post("/api/feedback", json=body, headers=BEARER)  # noqa: E731
    assert send({"item_id": "arxiv:1", "vote": 5}).status_code == 422
    assert send({"item_id": "x" * 500, "vote": 1}).status_code == 422
    assert send({"vote": 1}).status_code == 422
    assert send({"item_id": "arxiv:missing", "vote": 1}).status_code == 404


def test_token_never_appears_in_responses(db_path: Path) -> None:
    c = unlocked_client(db_path)
    pages = [c.get(p).text for p in ("/", "/changed", "/runs", "/graph")]
    assert all(TOKEN not in page for page in pages)
    assert TOKEN not in c.post("/api/feedback", json={"vote": 9}, headers=BEARER).text


def test_buttons_show_only_when_unlocked(db_path: Path) -> None:
    locked = writable_client(db_path)
    page = locked.get("/").text
    assert 'class="vote"' not in page and 'action="/unlock"' in page
    unlocked = unlocked_client(db_path)
    page = unlocked.get("/").text
    assert 'class="vote"' in page and 'action="/unlock"' not in page
    assert '<script src="/static/feedback.js"' in page


def test_no_buttons_or_unlock_form_in_demo_or_without_token(db_path: Path) -> None:
    seed_brief(db_path)
    for env in ({"SCOUT_DEMO": "1", "SCOUT_API_TOKEN": TOKEN}, {}):
        page = client_for(db_path, **env).get("/").text
        assert 'class="vote"' not in page and 'action="/unlock"' not in page


def test_current_vote_is_rendered_as_pressed(db_path: Path) -> None:
    c = unlocked_client(db_path)
    first = c.get("/").text.split('data-item="')[1].split('"')[0]
    c.post("/api/feedback", json={"item_id": first, "vote": 1}, headers=VOTE_HEADERS)
    assert 'aria-pressed="true"' in c.get("/").text


def test_every_write_route_is_guarded(db_path: Path) -> None:
    from fastapi.routing import APIRoute

    from scout.api.auth import require_write

    app = client_for(db_path).app
    unguarded = []
    for route in app.routes:  # type: ignore[attr-defined]
        if not isinstance(route, APIRoute) or route.methods <= {"GET", "HEAD"}:
            continue
        deps = {d.call for d in route.dependant.dependencies}
        if require_write not in deps and route.path != "/unlock":  # /unlock checks by hand
            unguarded.append(route.path)
    assert unguarded == []
