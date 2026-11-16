-- Schema v2: generation-based recompute for the local study.
-- v1 natural keys are unchanged. Additive statements only; the app also creates
-- these tables from SQLAlchemy metadata on an empty database.
--
-- Natural keys added:
--   run_generations(run_id, generation_no)
--   generation_parts(generation_id, observation_id, stage)
--   efficiency_runs.published_generation_no (publish compare-and-swap marker)
--   improvement_cases.source_generation_id -> run_generations(id)
--   efficiency_runs.published_generation_id / generation_count / algorithm

CREATE TABLE IF NOT EXISTS run_generations (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES efficiency_runs(id),
    generation_no INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    algorithm VARCHAR(80) NOT NULL,
    note VARCHAR(600) NOT NULL DEFAULT '',
    sensitivity_count INTEGER NOT NULL DEFAULT 3,
    snapshot JSON NOT NULL DEFAULT '{}',
    snapshot_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    selection JSON NOT NULL DEFAULT '{}',
    result JSON NOT NULL DEFAULT '{}',
    verification JSON NOT NULL DEFAULT '{}',
    failure_scope JSON NOT NULL DEFAULT '[]',
    error VARCHAR(600) NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    created_by VARCHAR(160) NOT NULL DEFAULT '',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    published_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT uq_run_generations_run_no UNIQUE (run_id, generation_no)
);
CREATE INDEX IF NOT EXISTS ix_run_generations_run_id ON run_generations (run_id);
CREATE INDEX IF NOT EXISTS ix_run_generations_status ON run_generations (status);

CREATE TABLE IF NOT EXISTS generation_parts (
    id SERIAL PRIMARY KEY,
    generation_id INTEGER NOT NULL REFERENCES run_generations(id),
    observation_id INTEGER NOT NULL,
    venue_code VARCHAR(60) NOT NULL,
    venue_name VARCHAR(140) NOT NULL,
    stage VARCHAR(20) NOT NULL DEFAULT 'baseline',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    input_fingerprint VARCHAR(64) NOT NULL DEFAULT '',
    result JSON NOT NULL DEFAULT '{}',
    error VARCHAR(600) NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    completed_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT uq_generation_parts_unit UNIQUE (generation_id, observation_id, stage)
);
CREATE INDEX IF NOT EXISTS ix_generation_parts_generation_id ON generation_parts (generation_id);
CREATE INDEX IF NOT EXISTS ix_generation_parts_status ON generation_parts (status);

ALTER TABLE efficiency_runs ADD COLUMN IF NOT EXISTS algorithm VARCHAR(80) NOT NULL DEFAULT 'ccr-input-two-stage-v1';
ALTER TABLE efficiency_runs ADD COLUMN IF NOT EXISTS published_generation_id INTEGER;
ALTER TABLE efficiency_runs ADD COLUMN IF NOT EXISTS published_generation_no INTEGER;
ALTER TABLE efficiency_runs ADD COLUMN IF NOT EXISTS generation_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE improvement_cases ADD COLUMN IF NOT EXISTS source_generation_id INTEGER REFERENCES run_generations(id);
ALTER TABLE improvement_cases ADD COLUMN IF NOT EXISTS source_evidence JSON;
ALTER TABLE improvement_cases ADD COLUMN IF NOT EXISTS migration_history JSON;

-- Pre-upgrade completed runs are adopted at application startup as a synthetic
-- published generation 1 (no recompute), so existing research and cases stay readable.
SELECT 'venue-frontier schema v2';
