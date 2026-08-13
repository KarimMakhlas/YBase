-- Resolution is a durable review decision, never a destructive graph update.
ALTER TABLE resolution_ledger
    ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE resolution_ledger
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE resolution_ledger
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE resolution_ledger
    ADD COLUMN IF NOT EXISTS resolved_by_user_id INT REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS resolution_ledger_workspace_status_idx
    ON resolution_ledger(workspace_id, status, created_at DESC);
