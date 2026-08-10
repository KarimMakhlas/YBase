-- Durable scheduling state: formation claims rotate across workspaces while
-- retaining FIFO order for documents within the selected workspace.
ALTER TABLE workspaces
    ADD COLUMN IF NOT EXISTS last_formation_served_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS workspaces_formation_fairness_idx
    ON workspaces(last_formation_served_at ASC NULLS FIRST, id);
