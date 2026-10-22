-- Typed entities and relations extracted with the summary (prompt v2). `entities` keeps plain names
-- so existing readers are unaffected.
ALTER TABLE summaries ADD COLUMN graph_json TEXT NOT NULL DEFAULT '{}';

-- One row per item: the newest summary, whichever prompt version produced it.
CREATE VIEW latest_summaries AS
SELECT s.* FROM summaries s
WHERE s.rowid = (
    SELECT x.rowid FROM summaries x WHERE x.item_id = s.item_id
    ORDER BY x.created_at DESC, x.prompt_version DESC LIMIT 1
);
