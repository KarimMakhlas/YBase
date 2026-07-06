-- Debounce queue for batch consolidation. Formation no longer merges
-- near-duplicate decisions inline after every document; it accumulates the
-- touched decision ids here and a worker claims the workspace's batch once
-- the debounce (or max-delay) elapses. One row per workspace; running_since
-- marks an in-flight run (reset by the janitor if the instance died).
CREATE TABLE IF NOT EXISTS consolidation_queue (
    workspace_id     INT PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    touched_ids      INT[] NOT NULL DEFAULT '{}',
    first_touched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_touched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    running_since    TIMESTAMPTZ
);
