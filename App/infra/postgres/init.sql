CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_events (
  event_id UUID PRIMARY KEY,
  review_kind TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'edited')),
  reviewer_id TEXT NOT NULL,
  evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_memory (
  memory_id UUID PRIMARY KEY,
  user_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT,
  memory_kind TEXT NOT NULL,
  value JSONB NOT NULL,
  source_session_id TEXT,
  confidence NUMERIC(4,3),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted', 'superseded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS user_memory_lookup_idx ON user_memory (user_id, status, updated_at DESC);
