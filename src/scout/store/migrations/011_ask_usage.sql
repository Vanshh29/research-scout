-- Real (non-cached) provider calls made for Ask, per UTC day. Capped separately from the shared
-- token budget so interactive questions cannot use up what the daily pipeline needs.
CREATE TABLE ask_usage (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL
);
