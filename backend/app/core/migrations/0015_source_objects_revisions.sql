-- Stable source identities and immutable content revisions. Documents remain a
-- compatibility projection so existing APIs and graph links keep their IDs.
CREATE TABLE IF NOT EXISTS source_objects (
    id SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    identity_key TEXT NOT NULL,
    source_connection_id INT REFERENCES source_connections(id) ON DELETE SET NULL,
    source_stream_id INT REFERENCES source_streams(id) ON DELETE SET NULL,
    external_ref TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'deleted', 'permission_lost')),
    external_updated_at TIMESTAMPTZ,
    current_revision_id INT,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, identity_key),
    UNIQUE (id, workspace_id)
);

CREATE TABLE IF NOT EXISTS document_revisions (
    id SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_object_id INT NOT NULL,
    revision_number INT NOT NULL,
    content_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    doc_created_at TIMESTAMPTZ,
    external_updated_at TIMESTAMPTZ,
    raw_text TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'materializing', 'searchable', 'failed', 'deleted')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT document_revisions_source_content_key UNIQUE (source_object_id, content_hash),
    UNIQUE (source_object_id, revision_number),
    UNIQUE (id, workspace_id),
    CONSTRAINT document_revisions_source_workspace_fk
        FOREIGN KEY (source_object_id, workspace_id)
        REFERENCES source_objects(id, workspace_id) ON DELETE CASCADE
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_object_id INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS revision_id INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

-- Existing documents predate revision tracking. Give each one a durable legacy
-- identity and immutable revision before enforcing the new projection links.
INSERT INTO source_objects(
    workspace_id, identity_key, source_connection_id, source_stream_id, external_ref
)
SELECT d.workspace_id, 'legacy:' || d.id::text, d.source_connection_id,
       d.source_stream_id, d.external_ref
FROM documents d
WHERE d.source_object_id IS NULL
ON CONFLICT (workspace_id, identity_key) DO NOTHING;

INSERT INTO document_revisions(
    workspace_id, source_object_id, revision_number, content_hash, source,
    title, author, doc_created_at, raw_text, tags, status
)
SELECT d.workspace_id, so.id, 1,
       COALESCE(d.content_hash, md5(d.source || E'\n' || d.title || E'\n' || d.raw_text)),
       d.source, d.title, d.author, d.doc_created_at, d.raw_text, d.tags,
       CASE WHEN d.formation_status = 'failed' THEN 'failed' ELSE 'searchable' END
FROM documents d
JOIN source_objects so ON so.workspace_id=d.workspace_id
    AND so.identity_key='legacy:' || d.id::text
WHERE d.revision_id IS NULL
ON CONFLICT (source_object_id, content_hash) DO NOTHING;

UPDATE documents d
SET source_object_id=so.id, revision_id=dr.id
FROM source_objects so
JOIN document_revisions dr ON dr.source_object_id=so.id
WHERE d.source_object_id IS NULL
  AND so.workspace_id=d.workspace_id
  AND so.identity_key='legacy:' || d.id::text;

UPDATE source_objects so
SET current_revision_id=dr.id
FROM document_revisions dr
WHERE so.current_revision_id IS NULL
  AND dr.source_object_id=so.id;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='source_objects_current_revision_fk') THEN
        ALTER TABLE source_objects ADD CONSTRAINT source_objects_current_revision_fk
            FOREIGN KEY (current_revision_id, workspace_id)
            REFERENCES document_revisions(id, workspace_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='documents_source_object_workspace_fk') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_source_object_workspace_fk
            FOREIGN KEY (source_object_id, workspace_id)
            REFERENCES source_objects(id, workspace_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='documents_revision_workspace_fk') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_revision_workspace_fk
            FOREIGN KEY (revision_id, workspace_id)
            REFERENCES document_revisions(id, workspace_id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS documents_revision_uidx ON documents(revision_id);
CREATE INDEX IF NOT EXISTS documents_active_workspace_idx
    ON documents(workspace_id, is_active) WHERE is_active;
