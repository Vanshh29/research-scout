CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,             -- ISO-8601 UTC
    finished_at   TEXT,                      -- NULL while running (or if the process died)
    status        TEXT NOT NULL,             -- running | ok | partial | failed | empty
    trigger       TEXT NOT NULL DEFAULT 'manual',
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    items_failed  INTEGER NOT NULL DEFAULT 0, -- sources that failed
    tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE source_status (
    run_id        INTEGER NOT NULL REFERENCES runs (id),
    source        TEXT NOT NULL,
    status        TEXT NOT NULL,             -- ok | failed
    error         TEXT,
    items_fetched INTEGER NOT NULL DEFAULT 0,
    items_new     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source)
);
