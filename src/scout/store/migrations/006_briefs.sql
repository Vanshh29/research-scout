-- Each delivered brief, kept as JSON so the dashboard can show "what changed today".
CREATE TABLE briefs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs (id),
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL                -- JSON of the Brief model
);

-- An item appears in at most one brief, so tomorrow's brief only shows what is new to the reader.
CREATE TABLE brief_items (
    item_id  TEXT PRIMARY KEY REFERENCES items (id),
    brief_id INTEGER NOT NULL REFERENCES briefs (id),
    rank     INTEGER NOT NULL
);
