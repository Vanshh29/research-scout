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

## Day 2 — 2026-10-19 · More sources, safety, daily workflow

**What changed**
- GitHub source: one search per topic, deduped by numeric repo id, ranked by star velocity
  (stars per day since creation). First live run: **121 repos**, second run 0 new.
- RSS/Atom source for the four verified blogs, with an age window and a per-feed cap.
- Sanitizer (`process/sanitize.py`): markup to text, control and invisible character removal,
  deterministic truncation, `<document>` delimiting that escapes break-out tags, http(s)-only
  links, and injection-signal heuristics. Every item passes through it before storage.
- Injection fixtures: a hostile feed (hidden script, fake `SYSTEM:` line, zero-width characters,
  a `</document>` break-out, a `javascript:` link) must come out inert and contained.
- `runs` and `source_status` tables, `scout run-daily`, `scout status`, `scout snapshot`.
- Daily workflow: restores the previous database, runs the pipeline, uploads the new snapshot.
  A manual run succeeded and published a 729 KB `scout.db`.
- ADR-007 (GitHub source), ADR-008 (untrusted content), ADR-002 amended.

**What surprised me**
- Hugging Face's feed has 881 entries and no summaries, and Latent Space ships 54 KB of HTML per
  post. That forced the age window, the per-feed cap and the 8000-character body limit.
- My first sanitizer test asserted "no `<` in the text", but real posts contain "<24GB". The
  test was wrong, not the code.
- A data branch would have meant force pushes or a growing binary history. A release asset
  replaced by the workflow avoids both.
- The macOS hidden `.pth` flag comes back between commands, so `just fix-pth` has to run right
  before `scout`.
- A pre-commit hook fixing a fixture's last line made one commit fail and my next `git add -A`
  folded two changes into one commit.

**What's next (Day 3)**
- LLM provider interface (Gemini, Anthropic, Fake), cache, budget guard, rate limiter.
- Summarizer with a versioned prompt, plus the canary test against the Fake provider.
- Local embeddings, hybrid ranking with score breakdown, `scout brief` and Telegram.
- Minimal `scout label`.
