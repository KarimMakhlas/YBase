-- Agent write-back: proposals from API-key-authenticated agents that a human
-- curator approves into real memory nodes (or rejects). Unlike formation,
-- nothing an agent proposes becomes live memory without a human gate — the
-- proposal row is the queue entry, and created_node_id records the outcome
-- so the proposing agent can look up the node it produced.
CREATE TABLE IF NOT EXISTS memory_proposals (
    id                BIGSERIAL PRIMARY KEY,
    workspace_id      INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    api_key_id        BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
    kind              TEXT NOT NULL DEFAULT 'decision' CHECK (kind IN ('decision', 'question')),
    label             TEXT NOT NULL,
    summary           TEXT NOT NULL,
    status_suggestion TEXT,
    topics            TEXT[] NOT NULL DEFAULT '{}',
    data              JSONB NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by       INT REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at       TIMESTAMPTZ,
    resolution_note   TEXT,
    created_node_id   INT REFERENCES memory_nodes(id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_proposals_queue_idx
    ON memory_proposals(workspace_id, status, created_at DESC);
