-- Response cache keyed by (prompt_version, model, content_hash): unchanged items never cost twice.
CREATE TABLE llm_cache (
    cache_key     TEXT PRIMARY KEY,          -- sha256 of prompt_version|model|content_hash
    prompt_version TEXT NOT NULL,
    model         TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    response      TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

-- One row per real (non-cached) provider call; the budget guard sums these per UTC day.
CREATE TABLE llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    day           TEXT NOT NULL,             -- YYYY-MM-DD, UTC
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
);
CREATE INDEX idx_llm_usage_day ON llm_usage (day);
