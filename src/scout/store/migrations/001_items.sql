CREATE TABLE items (
    id            TEXT PRIMARY KEY,          -- "{source}:{native_id}", see ADR-003
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    authors       TEXT NOT NULL,             -- JSON array
    published_at  TEXT,                      -- ISO-8601 UTC
    fetched_at    TEXT NOT NULL,
    body          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    raw_metadata  TEXT NOT NULL,             -- JSON object
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX idx_items_source ON items (source);
CREATE INDEX idx_items_first_seen ON items (first_seen_at);
