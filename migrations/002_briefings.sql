CREATE TABLE IF NOT EXISTS briefings (
  briefing_id TEXT PRIMARY KEY,
  trigger_reason TEXT NOT NULL,
  status TEXT NOT NULL,
  authoritative INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  model TEXT NOT NULL,
  input_snapshot_hash TEXT NOT NULL UNIQUE,
  input_watermark_utc TEXT NOT NULL,
  input_snapshot_json TEXT NOT NULL,
  first_seen_at_utc TEXT NOT NULL,
  requested_at_utc TEXT,
  completed_at_utc TEXT,
  available_to_system_at_utc TEXT,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  estimated_cost_usd REAL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  error_type TEXT,
  error_detail TEXT,
  output_json TEXT,
  content_hash TEXT,
  boot_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_briefings_requested ON briefings(requested_at_utc);
CREATE INDEX IF NOT EXISTS idx_briefings_available ON briefings(available_to_system_at_utc);
CREATE INDEX IF NOT EXISTS idx_briefings_status ON briefings(status);
