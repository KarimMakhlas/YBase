CREATE TABLE IF NOT EXISTS memory_events (
    id BIGSERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    node_id INT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('proposed','decided','revisited','reversed','reaffirmed','open','resolved')),
    effective_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (node_id, workspace_id) REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_events_node_effective_idx ON memory_events(node_id, effective_at DESC, id DESC);
