-- Decision signatures participate in semantic consolidation, so they must use
-- the same versioned embedding lifecycle as retrieval chunks. Keeping them in
-- a side-by-side relation prevents comparisons across model spaces during a
-- staged migration.
CREATE UNIQUE INDEX IF NOT EXISTS memory_nodes_id_workspace_idx
    ON memory_nodes(id, workspace_id);

CREATE TABLE IF NOT EXISTS memory_node_embeddings (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    node_id INT NOT NULL,
    embedding_model_id INT NOT NULL REFERENCES embedding_models(id) ON DELETE RESTRICT,
    embedding vector(512) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, embedding_model_id),
    CONSTRAINT memory_node_embeddings_node_workspace_fk
        FOREIGN KEY (node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_node_embeddings_workspace_model_idx
    ON memory_node_embeddings(workspace_id, embedding_model_id);
CREATE INDEX IF NOT EXISTS memory_node_embeddings_embedding_idx
    ON memory_node_embeddings USING hnsw (embedding vector_cosine_ops);

-- Existing node signatures belong to the workspace's currently active chunk
-- model. Unknown/empty workspaces remain unversioned until they receive data.
INSERT INTO memory_node_embeddings(workspace_id, node_id, embedding_model_id, embedding)
SELECT n.workspace_id, n.id, w.active_embedding_model_id, n.embedding
FROM memory_nodes n
JOIN workspaces w ON w.id=n.workspace_id
WHERE n.kind='decision' AND n.archived_at IS NULL AND n.embedding IS NOT NULL
  AND w.active_embedding_model_id IS NOT NULL
ON CONFLICT (node_id, embedding_model_id) DO NOTHING;
