# Architecture Decision Records

Short ADRs: Context / Options / Decision / Consequences.

---

## ADR-001: Core stack

**Context.** One-week build, solo, must be cheap to run, easy to test offline, and explainable.

**Options.** (a) Python + SQLite + FastAPI/HTMX; (b) Postgres + pgvector + React; (c) LangChain-based pipeline.

**Decision.** (a): Python 3.12, `uv`, SQLite (+ `sqlite-vec`), NetworkX built from SQLite tables,
FastAPI + Jinja2 + HTMX, `typer` CLI, `httpx`, `feedparser`. No agent framework.

**Consequences.** One file holds all state → trivial to snapshot, ship to the dashboard, and seed a
demo. Writing the pipeline by hand means every step is visible and explainable. Upgrade path:
Postgres + pgvector if the data outgrows a single file or needs concurrent writers.

---

## ADR-002: Hosting on Render free tier with a read-only DB snapshot

**Context.** Render's free web service sleeps after ~15 min idle and has no persistent disk on the
free instance. The pipeline runs in GitHub Actions, not on the web host.

**Options.** (a) Paid Render instance + disk (~$7+/mo); (b) Fly.io machine + volume (~$3/mo, card
required); (c) free Render service serving a DB snapshot produced by the Action.

**Decision.** (c). The daily Action runs the pipeline, publishes `scout.db` to a `data` branch, and
triggers Render's deploy hook; the app downloads the snapshot on boot.

**Consequences.** $0 hosting; dashboard is naturally read-only (matches demo mode). Cold start
~30–60 s on first visit. Feedback votes cannot be written to the snapshot — they're stored via a
token-gated endpoint in a small append-only store and merged in the next run (designed on Day 5).
Upgrade path: paid instance with a disk, or a hosted DB.

---

## ADR-003: Item identity and dedupe

**Context.** Re-running ingest must never duplicate items; the same content can reappear with a new
version suffix (arXiv `v2`) or a trailing-slash URL.

**Decision.** `id = "{source}:{native_id}"` where `native_id` is the arXiv ID *without* version,
GitHub `owner/repo`, or the normalized URL for RSS. `content_hash = sha256(normalized title + body)`.
Upsert on `id`; if `content_hash` changed, update the row and mark it for re-summarization.

**Consequences.** Idempotent ingest; arXiv revisions update in place rather than duplicating.
The content hash also keys the LLM cache, so unchanged items never cost tokens twice.

---

## ADR-004: Gemini as default LLM, behind a provider interface

**Context.** Budget ≈ $0 for the daily run. Gemini's free tier is the cheapest option but its
model list and quotas have changed several times in 2026.

**Decision.** Gemini (Flash-class model) is the default. A `LLMProvider` protocol has Gemini,
Anthropic, and a deterministic `Fake` implementation; model names and rate limits live in config,
never in code.

**Consequences.** A quota change is a config edit, not a code change. The Fake provider makes the
full pipeline testable offline and lets demo mode run with zero keys.

---

## ADR-005: RSS feed selection

**Context.** The spec lists five blogs; each feed must be verified before use.

**Decision.** Verified on 2026-10-18 (HTTP 200 + valid XML): Lilian Weng (`/index.xml`),
Simon Willison (`/atom/everything/`), Hugging Face (`/blog/feed.xml`), Latent Space (`/feed`).
**Dropped:** Anthropic engineering blog — no RSS/Atom feed exists (both candidate URLs return 404).

**Consequences.** Four blog sources. Adding an HTML scraper for feedless blogs is possible later but
is out of scope for the week (scrapers are brittle and need their own tests).

---

## ADR-006: sqlite-vec for vectors (spike result)

**Context.** sqlite-vec is a loadable SQLite extension; some Python builds disable extension
loading, which would break vector search on the host or in CI.

**Spike (2026-10-18).** On macOS (Python 3.12.8, SQLite 3.45.3) `sqlite_vec.load()` works and a
`vec0` KNN query returns correct neighbours. Linux/Docker was **not** verified yet (Docker daemon
was not running).

**Decision.** Keep sqlite-vec. When it's added on Day 4, a CI test loads it on `ubuntu-latest`,
which is the same environment as the daily cron.

**Consequences.** Fallback if loading fails anywhere: brute-force cosine in numpy over stored
embeddings — at <50k items × 384 dims that is ~20 MB and a few ms per query, so the fallback is
acceptable, just less elegant.

---

## ADR-007: GitHub source — search by topic, rank by star velocity

**Context.** "Interesting new repos" is not the same as "most-starred repos". All-time star
counts favour old projects; the goal is what's *taking off now*.

**Options.** (a) Trending page scraping — no API, brittle. (b) Search API with a creation-date
window per topic. (c) GraphQL star history per repo — accurate, but one call per repo.

**Decision.** (b): one search per topic (`topic:X stars:>=20 created:>=<30 days ago`), deduped
across topics. Velocity = stars ÷ age in days (age floored at 1 day), stored in `raw_metadata`.
Identity is the numeric repo id (`github:<id>`), so renames and transfers don't create duplicates.
Forks and archived repos are skipped. The body is description + topics; READMEs are not fetched
(one extra call per repo, and descriptions are enough for first-pass filtering).

**Consequences.** Velocity is a snapshot, not a time series: a 3-day-old repo with 300 stars and
a 29-day-old one with 300 stars rank very differently, which is the intent. Search allows 10
requests/min unauthenticated (30 with a token), so topics are spaced by a short sleep. Star
counts change daily but are deliberately not part of the content hash, so they don't trigger
"updated" every run. A later improvement is to fetch READMEs for the top-K only.

---

## ADR-008: Treat every fetched string as untrusted

**Context.** Papers, READMEs and blog posts reach LLM prompts and the dashboard. Any of them can
contain hidden instructions, markup, or characters that break prompt delimiters.

**Decision.** One gate, `process/sanitize.py`, applied in `ingest_sources` before anything is
stored: parsers convert markup to plain text; `sanitize_item` strips control, zero-width and
bidi-override characters, collapses title whitespace, and truncates the body to 8000 characters
at a word boundary (deterministic, marked with `…`). Non-http(s) links are dropped at parse time.
For prompts, `wrap_document` puts the text in `<document id=… source=…>` and escapes any literal
`<document` / `</document` inside it so content cannot close the block early.
`injection_signals` flags instruction-like phrases ("ignore previous instructions", fake
`SYSTEM:` lines, …) and stores them in `raw_metadata`.

**Consequences.** The signal list is a heuristic for visibility, never a filter: attackers can
rephrase, so the actual defense is delimiting plus a system prompt that says document text is
data (added with the LLM layer on Day 3, with a canary test against the Fake provider). Storing
truncated bodies means a long post is summarized from its first 8000 characters; acceptable for
triage, noted as a limitation.
