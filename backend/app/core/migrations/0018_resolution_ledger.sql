CREATE TABLE IF NOT EXISTS resolution_ledger (
    id BIGSERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    survivor_node_id INT NOT NULL,
    retired_node_id INT NOT NULL,
    similarity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','approved','rejected','reverted')),
    resolver_version TEXT NOT NULL DEFAULT 'v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (survivor_node_id, retired_node_id),
    FOREIGN KEY (survivor_node_id, workspace_id) REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE,
    FOREIGN KEY (retired_node_id, workspace_id) REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
