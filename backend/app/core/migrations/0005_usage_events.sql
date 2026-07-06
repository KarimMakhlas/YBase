-- Per-call LLM/embedding usage accounting, workspace-attributed via a
-- contextvar set at the pipeline entry points (formation / consolidation /
-- ingest / query). Token columns are nullable — providers that don't report
-- usage still contribute request_count. Pruned by the worker janitor past
-- USAGE_RETENTION_DAYS.
CREATE TABLE IF NOT EXISTS usage_events (
    id            BIGSERIAL PRIMARY KEY,
    workspace_id  INT REFERENCES workspaces(id) ON DELETE CASCADE,
    surface       TEXT NOT NULL,   -- formation | consolidation | query | ingest | unknown
    kind          TEXT NOT NULL,   -- llm | embedding
    provider      TEXT NOT NULL,   -- anthropic | nvidia | ollama | voyage | local
    model         TEXT NOT NULL,
    input_tokens  INT,
    output_tokens INT,
    total_tokens  INT,
    request_count INT NOT NULL DEFAULT 1,
    document_id   INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS usage_events_ws_created_idx
    ON usage_events(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_created_idx
    ON usage_events(created_at);
