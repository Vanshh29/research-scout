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
