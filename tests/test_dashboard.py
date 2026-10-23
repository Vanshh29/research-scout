import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scout.api import views
from scout.api.app import ago, create_app, duration, safe_url, when
from scout.brief import build_brief, record_brief
from scout.config import Settings
from scout.ingest.run import SourceReport
from scout.store.db import connect, migrate, upsert_item
from scout.store.runs import finish_run, start_run
from tests.test_brief import ranked
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
    # migrations 008 and 009 are not idempotent; an older snapshot lacks their tables entirely
    conn = connect(path)
    for table in ("graph_built", "edges", "entities"):
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
