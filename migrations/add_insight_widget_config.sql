-- Home Insights widget preferences.
--
-- Klearing has no login identity: one data directory is one local profile.
-- profile_id keeps that scope explicit without adding unused authentication
-- infrastructure, and leaves room for multiple profiles later.
--
-- The ordered JSON list is atomic and intentionally permits []. No row means
-- "new profile, use the three product defaults"; [] means the user explicitly
-- chose an empty Home widget. Metric IDs are code-owned and validated before
-- every write, so there is no database table to reference with a foreign key.
CREATE TABLE IF NOT EXISTS insight_widget_config (
  profile_id TEXT PRIMARY KEY,
  metric_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
