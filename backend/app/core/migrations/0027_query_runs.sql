-- Durable query quality/latency observations. No question/answer text is
-- stored here: this is operational telemetry, not another memory corpus.
CREATE TABLE IF NOT EXISTS query_runs (
    id BIGSERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('success', 'retrieval_error', 'llm_error')),
    retrieval_ms INT,
    generation_ms INT,
    verification_ms INT,
    total_ms INT,
    retrieved_chunks INT NOT NULL DEFAULT 0,
    valid_citations INT NOT NULL DEFAULT 0,
    citation_coverage REAL,
    confidence TEXT,
    claim_verification_status TEXT,
    unsupported_claims INT NOT NULL DEFAULT 0,
    contradicted_claims INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS query_runs_workspace_created_idx
    ON query_runs(workspace_id, created_at DESC);
