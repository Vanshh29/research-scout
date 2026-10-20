CREATE TABLE summaries (
    item_id        TEXT NOT NULL REFERENCES items (id),
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    content_hash   TEXT NOT NULL,           -- hash of the item content that was summarized
    tldr           TEXT NOT NULL,
    key_points     TEXT NOT NULL,           -- JSON array
    entities       TEXT NOT NULL,           -- JSON array
    created_at     TEXT NOT NULL,
    PRIMARY KEY (item_id, prompt_version)
);
