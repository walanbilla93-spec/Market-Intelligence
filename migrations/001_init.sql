PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS boots (
  boot_id TEXT PRIMARY KEY,
  started_at_utc TEXT NOT NULL,
  version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  title TEXT NOT NULL,
  scheduled_at_utc TEXT,
  publisher_time_utc TEXT,
  first_seen_at_utc TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  available_to_system_at_utc TEXT NOT NULL,
  verification_status TEXT NOT NULL,
  publisher TEXT NOT NULL,
  source_url TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_revisions (
  revision_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  scheduled_at_utc TEXT,
  content_hash TEXT NOT NULL,
  source_url TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS observations (
  observation_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  observed_at_utc TEXT NOT NULL,
  available_to_system_at_utc TEXT NOT NULL,
  metric TEXT NOT NULL,
  instrument TEXT,
  value_num REAL,
  unit TEXT,
  status TEXT NOT NULL,
  source_url TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  boot_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  last_attempt_at_utc TEXT NOT NULL,
  last_success_at_utc TEXT,
  status TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL,
  latency_ms INTEGER,
  detail TEXT,
  boot_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_imports (
  candidate_id TEXT PRIMARY KEY,
  candidate_key TEXT,
  episode_id TEXT,
  decision_at_utc TEXT NOT NULL,
  config_hash TEXT,
  imported_at_utc TEXT NOT NULL,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer_outputs (
  output_id TEXT PRIMARY KEY,
  candidate_id TEXT,
  observed_at_utc TEXT NOT NULL,
  available_to_system_at_utc TEXT NOT NULL,
  model TEXT NOT NULL,
  authoritative INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  boot_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_scheduled ON events(scheduled_at_utc);
CREATE INDEX IF NOT EXISTS idx_events_available ON events(available_to_system_at_utc);
CREATE INDEX IF NOT EXISTS idx_observations_time ON observations(observed_at_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_decision ON candidate_imports(decision_at_utc);

