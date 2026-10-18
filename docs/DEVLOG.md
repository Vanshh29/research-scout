# Dev Log

## Day 1 — 2026-10-18 · Foundation + arXiv ingest

**What changed**
- Project scaffold: uv, ruff, mypy (strict), pytest, pre-commit; CI on GitHub Actions (green).
- Plan, architecture, data model, and ADRs 001–006 in `docs/`.
- Config: env settings via pydantic-settings; `sources.yaml` and `profile.yaml` validated
  (ranking weights must sum to 1, feed URLs must be http(s)).
- SQLite store with numbered SQL migrations (`PRAGMA user_version`) and an idempotent upsert
  that returns new / updated / unchanged.
- JSON logging that redacts any field that looks like a key or token.
- HTTP client with full-jitter exponential backoff and `Retry-After`; no retries on 4xx.
- arXiv source + `scout ingest`: first live run stored **124 papers**; second run **0 new** (idempotent).

**What surprised me**
- The Anthropic engineering blog has no RSS feed — dropped it (ADR-005).
- On macOS, an iCloud-synced Desktop folder marks uv's editable-install `.pth` as *hidden*, and
  Python 3.12+ silently skips hidden `.pth` files → `ModuleNotFoundError`. Fixed with
  `pythonpath` for pytest and a `just fix-pth` step.
- Naive keyword matching is wrong: "RAG" matches "storage" and "average". Switched to
  word-bounded regex with optional plural.
- Render's free tier has no persistent disk, which pushed the design toward "the cron produces a
  DB snapshot, the dashboard serves it read-only" (ADR-002). That turned out simpler anyway.

**What's next (Day 2)**
- GitHub source (topics + star velocity) and RSS source.
- Sanitizer + prompt-injection fixtures.
- `runs` / `source_status` tables and `scout run-daily`.
- Daily cron Action that ingests and publishes the DB snapshot to a `data` branch.
