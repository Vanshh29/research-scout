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

## Day 3 — 2026-10-20 · LLM layer, ranking, brief, labels

**What changed**
- `llm/`: provider protocol with Gemini, Anthropic (both plain httpx) and a deterministic Fake;
  response cache, daily token/cost budget guard, rate limiter. Keys travel in headers, never URLs.
- Summarizer with `prompts/summarize_v1.md`: untrusted text goes through `wrap_document`, replies are
  validated JSON, bad replies are rejected and not cached. Canary tests reuse the Day 2 poisoned feed.
- Ranking: fastembed (`bge-small`) embeddings, rubric LLM score for the top-K, hybrid blend with the
  full breakdown stored. Real run: 561 items embedded locally; the top results were RAG, tool-using
  agents, and agent evaluation papers.
- `scout brief`, Telegram (HTML, escaped) and the optional n8n webhook. `run-daily` now chains
  ingest, summarize, rank, brief, deliver; writes tokens and cost to the run row; sends a failure alert.
- `scout label` (relevance 0-3, summary ratings 1-5), items shown in a fixed pseudo-random order so
  the label set is not biased toward top-ranked items.
- ADR-010 (fastembed, httpx providers), ADR-011 (cache, budget, two-stage ranking). 137 tests, all offline.

**What surprised me**
- Caching the raw model reply is a trap: a malformed reply would be replayed forever. The client now
  caches only after validation (found by a canary test, not by thinking ahead).
- A 10 requests/minute limiter made the Fake provider crawl; offline runs now skip it.
- The Mac booted at 13:06, so launchd never fired the 13:00 agent (it does not replay missed runs
  after a shutdown), and a second launchd problem reads scripts from Desktop (macOS privacy).

**Not done / waiting on me**
- No real Gemini or Telegram run yet: no keys. Everything is tested against Fake and mocked transports.
- The 30 hand labels have not started.

**What's next (Day 4)**
- sqlite-vec storage (CI must load it on ubuntu), semantic search, "more like this".
- Entity and relation extraction, graph build and queries.
