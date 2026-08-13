-- Evidence may support a relationship target (person/topic) without making it
-- the observation's primary field projection. Keep that lineage separately so
-- reformation can retire every derived chunk link without rebuilding support
-- nodes as if they were the decision/question/entity observation itself.
CREATE TABLE IF NOT EXISTS observation_support_projections (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    observation_id BIGINT NOT NULL,
    node_id INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, node_id),
    CONSTRAINT observation_support_projections_observation_workspace_fk
        FOREIGN KEY (observation_id, workspace_id)
        REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT observation_support_projections_node_workspace_fk
        FOREIGN KEY (node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observation_support_projections_node_idx
    ON observation_support_projections(node_id);
