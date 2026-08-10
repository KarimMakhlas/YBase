-- Side-by-side chunk vectors.  The legacy chunks.embedding column remains
-- readable during rollout; queries will move to this relation in a later step.
CREATE TABLE IF NOT EXISTS embedding_models (
    id SERIAL PRIMARY KEY,
    model_key TEXT NOT NULL UNIQUE,
    dimension INT NOT NULL DEFAULT 512 CHECK (dimension = 512),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS active_embedding_model_id INT;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='workspaces_active_embedding_model_fk') THEN
        ALTER TABLE workspaces ADD CONSTRAINT workspaces_active_embedding_model_fk
            FOREIGN KEY (active_embedding_model_id) REFERENCES embedding_models(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    chunk_id INT NOT NULL,
    embedding_model_id INT NOT NULL REFERENCES embedding_models(id) ON DELETE RESTRICT,
    embedding vector(512) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, embedding_model_id),
    CONSTRAINT chunk_embeddings_chunk_workspace_fk
        FOREIGN KEY (chunk_id, workspace_id) REFERENCES chunks(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS chunk_embeddings_workspace_model_idx
    ON chunk_embeddings(workspace_id, embedding_model_id);
CREATE INDEX IF NOT EXISTS chunk_embeddings_embedding_idx
    ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);

-- Preserve all existing vectors as named versions before any caller moves to
-- the relation. Unknown legacy rows stay explicitly identifiable for repair.
INSERT INTO embedding_models(model_key)
SELECT DISTINCT COALESCE(NULLIF(embed_model, ''), 'legacy:unknown') FROM chunks
ON CONFLICT (model_key) DO NOTHING;

INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding)
SELECT c.workspace_id, c.id, m.id, c.embedding
FROM chunks c
JOIN embedding_models m ON m.model_key=COALESCE(NULLIF(c.embed_model, ''), 'legacy:unknown')
ON CONFLICT (chunk_id, embedding_model_id) DO NOTHING;

UPDATE workspaces w SET active_embedding_model_id=(
    SELECT ce.embedding_model_id
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id=ce.chunk_id
    JOIN documents d ON d.id=c.document_id
    WHERE ce.workspace_id=w.id AND d.is_active
    GROUP BY ce.embedding_model_id
    ORDER BY count(*) DESC, ce.embedding_model_id
    LIMIT 1
)
WHERE w.active_embedding_model_id IS NULL;
