-- Field-level lineage for automated primary memory projections. Historical rows
-- are retained; a field is active only when its observation/run is active.
CREATE TABLE IF NOT EXISTS memory_field_projections (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    observation_id BIGINT NOT NULL,
    node_id INT NOT NULL,
    field_name TEXT NOT NULL CHECK (field_name IN ('label', 'summary', 'status', 'data')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, node_id, field_name),
    CONSTRAINT memory_field_projections_observation_workspace_fk
        FOREIGN KEY (observation_id, workspace_id)
        REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT memory_field_projections_node_workspace_fk
        FOREIGN KEY (node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_field_projections_node_idx
    ON memory_field_projections(node_id, field_name);
