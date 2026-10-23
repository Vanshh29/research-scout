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

**Amendment (2026-10-19).** The snapshot is published as a **GitHub Release asset** (tag `data`,
file `scout.db`, replaced with `--clobber`) instead of a `data` branch. A binary committed daily
bloats git history, and avoiding history rewrites on that branch means no force pushes at all. The
workflow downloads the previous asset before each run so the database accumulates, and `scout
snapshot` (`VACUUM INTO`) produces a compact standalone copy to upload. The public URL is
`https://github.com/Vanshh29/research-scout/releases/download/data/scout.db`.

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

**Update (Day 4).** Implemented in `store/vectors.py`. The `vec0` table is *not* a migration:
`migrate()` runs on every command, so a failing extension would break the whole CLI. The index is a
derived cache in `data/vectors.db`, rebuilt from the `embeddings` table and never shipped in the
snapshot. `auto` falls back to numpy with a warning. `tests/test_vectors.py` fails (not skips) if
sqlite-vec cannot load, so CI on ubuntu-latest enforces the promise above, and a test asserts both
backends return the same top-k.

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

---

## ADR-009: n8n as an optional delivery layer, not a replacement for the pipeline

**Context.** n8n (visual workflow automation) is a common requirement in automation and
AI-workflow roles. Moving ranking or summarization into it would make them hard to test, version
and evaluate.

**Options.** (a) Rebuild the pipeline in n8n. (b) Skip n8n. (c) Keep the pipeline in Python and let
n8n handle routing: brief delivery, failure alerts, feedback buttons.

**Decision.** (c). `scout brief` POSTs the brief as JSON to `SCOUT_N8N_WEBHOOK_URL` when it is set
and does nothing when it isn't. Python Telegram delivery stays the default. Importable workflow
exports live in `n8n/` with no credentials inside, documented in `docs/n8n.md`.

**Consequences.** Tests, evals and the cost guard stay in code. n8n needs a host (Docker locally or
n8n Cloud); the free Render tier is unsuitable, and GitHub Actions cannot reach a local instance, so
the live demo is a recording or a cloud webhook. Workflows are a bonus and the first item cut if the
schedule slips.

---

## ADR-010: fastembed (ONNX) for local embeddings; LLM providers over plain httpx

**Context.** The stack line said sentence-transformers. It pulls in torch (1-2 GB), which would slow
CI and the daily GitHub Action and make the free-tier cron fragile. Separately, the official Gemini
and Anthropic SDKs are extra dependencies for what is one POST call each.

**Options.** (a) sentence-transformers. (b) fastembed: ONNX runtime, no torch, same class of small
embedding models. (c) Hash embeddings only. For providers: (a) vendor SDKs, (b) REST via httpx.

**Decision.** fastembed with `BAAI/bge-small-en-v1.5` (384 dims, about 130 MB, downloaded once and
cached in the workflow). A deterministic `HashEmbedder` serves tests and the Fake provider. Gemini and
Anthropic are called through httpx, reusing the project's backoff helper and `MockTransport` test
pattern. API keys go in headers, never URLs.

**Consequences.** Light installs and fully offline tests. We own the request/response parsing, so a
provider API change breaks us until we update it; fixtures pin today's shapes. The embedding score
maps cosine similarity linearly from a model-specific range (`score_range`) to 0..1; those constants
are calibrated by eye on real data and should be revisited once labels exist (Day 6).

---

## ADR-011: LLM cache, budget guard and two-stage ranking

**Context.** The daily run must be cheap, repeatable and honest about what it skipped.

**Decision.**
- Cache key is `sha256(prompt_version | model | content_hash)` (ADR-003). A reply is cached only
  after it passes validation, so a malformed reply is paid for once and never replayed.
- Budget guard: before each call, estimate tokens (about 4 characters each) plus the output cap and
  compare with today's recorded usage (`llm_usage`, per UTC day). Over the token or cost cap raises
  `BudgetExceeded`; stages stop, flag it, and the brief says so. Cached calls cost nothing.
- Rate limiter: minimum interval between calls from `requests_per_minute`; disabled for Fake.
- Ranking is two-stage: a free embedding score over items first seen in the last 7 days, then the LLM
  rubric (relevance, novelty, evidence quality, actionability, weighted by `profile.yaml`) for the
  top-K only. `hybrid = w_emb * embed + w_llm * llm`; items without an LLM score keep their embedding
  score. The full breakdown is stored in `scores`.
- An item appears in at most one brief (`brief_items`); a failed Telegram send does not mark items
  shown, so the next run retries them.

**Consequences.** The token estimate is rough, so the cap is approximate, not exact. First-pass
filtering uses embeddings instead of a cheap LLM, which saves quota but can miss items whose title
looks off-topic. Using one model for summaries and scores keeps config simple; a stronger model for
top-K is a config change away (`gemini_strong_model` exists but is not wired yet).

---

## ADR-012: Vector index, extraction and knowledge graph (Day 4)

**Context.** Day 4 adds semantic search, entity and relation extraction, and a graph with queries.
Constraints: no extra daily LLM calls (the cron limit is 20 minutes), nothing may break where
sqlite-vec cannot load, and every choice must be explainable.

**Decision.**
- **Vectors.** sqlite-vec `vec0` in a separate, rebuildable file (`data/vectors.db`), never in a
  migration and never in the snapshot, with a numpy fallback. `embeddings` stays the source of
  truth. (Details in the ADR-006 update.)
- **Extraction is folded into `summarize_v2`.** One LLM call per item returns the summary plus typed
  entities (Concept, Method, Repo, Org, with aliases) and relations (builds_on, implements,
  mentions, related_to). The parser drops junk entries instead of failing the summary, keeps only
  relations between listed entities, and still accepts v1 replies. Authors come from item metadata,
  not the LLM.
- **`latest_summaries` view.** `summaries` is keyed by prompt version, so a v2 run leaves a v1 row
  too. The view returns the newest row per item and all readers (brief, ranking, label tool) use it;
  without it `scout label` would have listed every re-summarized item twice.
- **Graph in tables, not pickled.** `entities` and `edges` (each edge carries its item) are rebuilt
  into NetworkX on demand. Rebuilding one item deletes and re-inserts only its edges, so
  `build-graph` is idempotent and skips unchanged summaries.
- **Alias merging by normalization.** Case, punctuation and hyphen-versus-space are ignored and a
  trailing " v2" is dropped, but bare numbers stay ("GPT-4" is not "GPT-4o"). A new name merges into
  an existing entity only if all its known forms point at one entity; an alias that matches two
  entities is ignored. `config/aliases.yaml` holds curated seeds.
- **Matching ignores the entity type.** The first idea was identity = (type, name), but the model
  typed the same thing as Method in one item and Concept in another, which split one node in two.
  The first-seen type wins.
- **Trending** compares the last N days with the N days before, dated by `published_at` (else
  `first_seen_at`), with a minimum-count floor. With no earlier mentions a concept is labelled
  "new" instead of getting an invented growth rate.

**Consequences.**
- Matching by normalization misses true synonyms ("RAG" and "retrieval-augmented generation" are
  only merged because they are seeded). Upgrade path: embed entity names and merge above a
  similarity threshold, with the seeds as a safety net.
- Item nodes and extracted entities are separate: the GitHub item `owner/project` and an extracted
  Repo `project` are two nodes. Upgrade path: link a Repo entity to the item whose URL or title it
  matches.
- With few items every paper's concepts form a clique, so clusters are mostly one item each. They
  become topical only after a few weeks of data.
- LLM entity quality is noisy ("AWS Trainium" typed as Method). It is measured against hand labels
  on Day 6, not tuned by feel.
- v2 costs about 960 tokens per item (measured on 60 real items), so 60 items a day is about 58k of
  the 200k token budget.

## ADR-013: Graph explorer with vendored Cytoscape.js

**Context.** The dashboard needs an interactive view of the knowledge graph. The CSP is
`script-src 'self'`, so nothing may load from a CDN or run inline, and the project has no JS build step.

**Options.** Cytoscape.js, D3 force layout, or a server-rendered static SVG.

**Decision.** Cytoscape.js 3.30.2 (MIT), copied into `web/static/vendor/` with its license. D3 would
mean writing layout, zoom and hit-testing by hand; static SVG cannot recentre on click. The page is a plain
GET form plus about 80 lines of `graph.js`; HTMX was dropped from the original plan because a form and one
`fetch` need no extra dependency. `/api/graph` returns a bounded slice (`subgraph()`, 80 nodes by default,
`truncated` flag, `depth` 1-2, `center` max 100 chars), so the page stays fast as the graph grows. Entity
names and titles are untrusted: they reach the DOM only through `textContent` or Cytoscape data fields,
and item URLs pass `safe_url` on the server.

**Consequences.**
- The vendored file is 370 KB in git. Upgrade path: pin it through a lockfile and a fetch step in CI.
- The layout (`cose`) runs in the browser on up to 80 nodes; much larger slices would need a server-side layout.
- Tests cover the JSON contract and hostile input, but not the drawing itself. That is checked by hand.

## ADR-014: Write guard and dashboard feedback

**Context.** The dashboard had no write path. Feedback votes are the first one, and the public demo must
stay read-only. The hosted app serves a snapshot that each deploy replaces (ADR-002), so a vote cast
there would be lost.

**Decision.**
- **Default-deny guard.** `require_write` refuses with 403 in demo mode (even with a valid token) and when
  `SCOUT_API_TOKEN` is unset, so a deploy that forgets the token has no open writes. A test walks
  `app.routes` and fails if any non-GET route lacks the guard (only `/unlock` is exempt, because it is how
  a browser gets credentials).
- **Two ways in.** `Authorization: Bearer <token>` for scripts. For browsers, `POST /unlock` checks the
  token and sets an `HttpOnly`, `SameSite=Strict` cookie holding an HMAC of the token, never the token.
  Comparisons use `secrets.compare_digest`.
- **CSRF.** Cookie-authenticated writes also need an `X-Scout: 1` header, which a cross-site form cannot
  send; bearer requests cannot be forged cross-site, so they skip it.
- **Feedback** is one row per item (`feedback`, migration 010); voting the same way again clears it. Vote
  buttons and the unlock form render only when writes are enabled, so the demo shows no dead controls.

**Consequences.**
- Votes persist only for a local `scout serve` against the real DB. Upgrade path: publish the feedback
  rows with the snapshot, or send hosted votes to a queue the daily run drains.
- No rate limit on `/unlock`. The token is long and random (`secrets.token_urlsafe(32)`), which makes
  guessing impractical, but a reverse proxy limit would be the next step if this were exposed widely.
- Votes do not change ranking yet. Planned (Rocchio): liked item vectors join the profile entries, disliked
  ones subtract a capped penalty; ship only if the eval harness shows it helps.
