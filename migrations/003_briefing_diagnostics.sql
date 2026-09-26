CREATE TABLE IF NOT EXISTS briefing_diagnostics (
  briefing_id TEXT PRIMARY KEY REFERENCES briefings(briefing_id),
  finish_reason TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  diagnostics_json TEXT NOT NULL DEFAULT '{}',
  updated_at_utc TEXT NOT NULL
);
