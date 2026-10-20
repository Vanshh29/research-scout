CREATE TABLE embeddings (
    item_id      TEXT NOT NULL REFERENCES items (id),
    model        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector       BLOB NOT NULL,             -- float32, L2-normalized
    PRIMARY KEY (item_id, model)
);

CREATE TABLE scores (
    item_id       TEXT PRIMARY KEY REFERENCES items (id),
    content_hash  TEXT NOT NULL,
    embed_score   REAL NOT NULL,            -- 0-1
    llm_score     REAL,                     -- 0-1, NULL if not LLM-scored (outside top-K, budget, error)
    llm_breakdown TEXT,                     -- JSON: rubric dimension -> 0-1
    llm_reason    TEXT,
    hybrid        REAL NOT NULL,            -- 0-1; equals embed_score when llm_score is NULL
    weights       TEXT NOT NULL,            -- JSON of the weights used
    prompt_version TEXT,
    model         TEXT,
    scored_at     TEXT NOT NULL
);
CREATE INDEX idx_scores_hybrid ON scores (hybrid DESC);
