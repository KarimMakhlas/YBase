-- Make chunk tenant ownership explicit so retrieval can filter before ANN
-- candidate selection and the database rejects cross-workspace provenance.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS workspace_id INT;

UPDATE chunks c
SET workspace_id = d.workspace_id
FROM documents d
WHERE d.id = c.document_id
  AND c.workspace_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS documents_id_workspace_uidx
    ON documents(id, workspace_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chunks_document_workspace_fk'
    ) THEN
        ALTER TABLE chunks
            ADD CONSTRAINT chunks_document_workspace_fk
            FOREIGN KEY (document_id, workspace_id)
            REFERENCES documents(id, workspace_id)
            ON DELETE CASCADE
            NOT VALID;
    END IF;
END $$;

ALTER TABLE chunks VALIDATE CONSTRAINT chunks_document_workspace_fk;
ALTER TABLE chunks ALTER COLUMN workspace_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS chunks_workspace_model_idx
    ON chunks(workspace_id, embed_model);
