-- One row per formation attempt outcome (success / failed / timeout), with
-- per-stage timings, for SLO reporting (P50/P95 latency, queue wait) and
-- fleet-accurate stall detection. Also the source for daily formation-quota
-- accounting. Pruned by the worker janitor past FORMATION_RUNS_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS formation_runs (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id   INT REFERENCES documents(id) ON DELETE SET NULL,
    status        TEXT NOT NULL,          -- success | failed | timeout
    attempt       INT NOT NULL DEFAULT 1,
    queue_wait_ms INT,
    duration_ms   INT,
    stage_timings JSONB NOT NULL DEFAULT '{}',
    error         TEXT,
    llm_provider  TEXT,
    llm_model     TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS formation_runs_ws_started_idx
    ON formation_runs(workspace_id, started_at DESC);
CREATE INDEX IF NOT EXISTS formation_runs_started_idx
    ON formation_runs(started_at);
