-- One thumbs up (1) or down (-1) per item, from the dashboard. A repeat vote clears the row.
CREATE TABLE feedback (
    item_id    TEXT PRIMARY KEY REFERENCES items (id),
    vote       INTEGER NOT NULL CHECK (vote IN (-1, 1)),
    created_at TEXT NOT NULL
);
