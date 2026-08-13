-- Durable preprocessing state. Accepted revisions are provider-free and can be
-- safely claimed/recovered separately from serialized canonical formation.
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS materialization_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS materialization_next_attempt_at TIMESTAMPTZ;
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS materialization_claimed_at TIMESTAMPTZ;
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS materialized_at TIMESTAMPTZ;
ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS last_materialization_served_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS document_revisions_materialization_claim_idx
    ON document_revisions(workspace_id, status, materialization_next_attempt_at, id)
    WHERE status='accepted';
CREATE INDEX IF NOT EXISTS workspaces_materialization_fairness_idx
    ON workspaces(last_materialization_served_at ASC NULLS FIRST, id);
