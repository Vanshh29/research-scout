-- Knowledge graph. Nodes are extracted concepts plus one node per item and per author; edges carry
-- the item they came from, so a rebuild for one item can replace exactly its own edges.
CREATE TABLE entities (
    id            INTEGER PRIMARY KEY,
    type          TEXT NOT NULL,            -- Concept, Method, Repo, Org, Person, Paper, Post
    name          TEXT NOT NULL,            -- display name
    norm_name     TEXT NOT NULL,            -- normalized for matching; the item id for item nodes
    aliases       TEXT NOT NULL DEFAULT '[]',   -- JSON array of other surface forms
    item_id       TEXT REFERENCES items (id),   -- set only for item nodes
    first_seen_at TEXT NOT NULL,
    UNIQUE (type, norm_name)
);

CREATE TABLE edges (
    src        INTEGER NOT NULL REFERENCES entities (id),
    dst        INTEGER NOT NULL REFERENCES entities (id),
    relation   TEXT NOT NULL,               -- mentions, builds_on, implements, related_to, authored_by
    item_id    TEXT NOT NULL REFERENCES items (id),
    created_at TEXT NOT NULL,
    UNIQUE (src, dst, relation, item_id)
);
CREATE INDEX idx_edges_item ON edges (item_id);
CREATE INDEX idx_edges_dst ON edges (dst);

-- Which summary each item's edges were built from, so unchanged items are skipped.
CREATE TABLE graph_built (
    item_id            TEXT PRIMARY KEY REFERENCES items (id),
    summary_created_at TEXT NOT NULL
);
