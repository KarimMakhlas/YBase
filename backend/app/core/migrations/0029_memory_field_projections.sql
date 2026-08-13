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

-- Existing primary observation projections already encode the compatible
-- provenance model. Backfill their fields before the runtime starts checking
-- completeness, so this additive migration does not falsely flag legacy data.
INSERT INTO memory_field_projections(workspace_id, observation_id, node_id, field_name)
SELECT op.workspace_id, op.observation_id, op.node_id, fields.field_name
FROM observation_projections op
JOIN memory_observations o ON o.id=op.observation_id
CROSS JOIN LATERAL unnest(
    CASE o.kind
        WHEN 'entity' THEN ARRAY['label', 'summary', 'data']::text[]
        ELSE ARRAY['label', 'summary', 'status', 'data']::text[]
    END
) AS fields(field_name)
ON CONFLICT DO NOTHING;
