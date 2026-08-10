-- Immutable formation candidates and their source observations.  The existing
-- graph tables remain a compatibility projection; these tables retain exactly
-- what an extraction proposed and which document chunks support it.

ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS revision_id INT;
ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS prompt_version TEXT;
ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS formation_runs_id_workspace_uidx
    ON formation_runs(id, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS chunks_id_workspace_uidx
    ON chunks(id, workspace_id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_nodes_id_workspace_uidx
    ON memory_nodes(id, workspace_id);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='formation_runs_revision_workspace_fk') THEN
        ALTER TABLE formation_runs ADD CONSTRAINT formation_runs_revision_workspace_fk
            FOREIGN KEY (revision_id, workspace_id)
            REFERENCES document_revisions(id, workspace_id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS formation_runs_one_active_revision_idx
    ON formation_runs(revision_id) WHERE is_active AND revision_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_observations (
    id               BIGSERIAL PRIMARY KEY,
    formation_run_id BIGINT NOT NULL,
    workspace_id     INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id      INT NOT NULL,
    revision_id      INT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('decision', 'entity', 'question')),
    ordinal          INT NOT NULL CHECK (ordinal >= 0),
    payload          JSONB NOT NULL,
    confidence       REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    effective_at     TIMESTAMPTZ,
    model_provider   TEXT,
    model_name       TEXT,
    prompt_version   TEXT,
    status           TEXT NOT NULL DEFAULT 'valid'
        CHECK (status IN ('valid', 'quarantined', 'retired')),
    quarantine_reason TEXT,
    retired_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, workspace_id),
    UNIQUE (formation_run_id, ordinal),
    CONSTRAINT memory_observations_run_workspace_fk
        FOREIGN KEY (formation_run_id, workspace_id)
        REFERENCES formation_runs(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT memory_observations_document_workspace_fk
        FOREIGN KEY (document_id, workspace_id)
        REFERENCES documents(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT memory_observations_revision_workspace_fk
        FOREIGN KEY (revision_id, workspace_id)
        REFERENCES document_revisions(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS memory_observations_active_revision_idx
    ON memory_observations(revision_id, status) WHERE status = 'valid';

CREATE TABLE IF NOT EXISTS observation_evidence (
    workspace_id    INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    observation_id  BIGINT NOT NULL,
    chunk_id        INT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, chunk_id),
    CONSTRAINT observation_evidence_observation_workspace_fk
        FOREIGN KEY (observation_id, workspace_id)
        REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT observation_evidence_chunk_workspace_fk
        FOREIGN KEY (chunk_id, workspace_id)
        REFERENCES chunks(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observation_evidence_chunk_idx ON observation_evidence(chunk_id);

CREATE TABLE IF NOT EXISTS observation_projections (
    workspace_id    INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    observation_id  BIGINT NOT NULL,
    node_id         INT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, node_id),
    CONSTRAINT observation_projections_observation_workspace_fk
        FOREIGN KEY (observation_id, workspace_id)
        REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT observation_projections_node_workspace_fk
        FOREIGN KEY (node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observation_projections_node_idx ON observation_projections(node_id);

CREATE TABLE IF NOT EXISTS observation_edge_projections (
    workspace_id    INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    observation_id  BIGINT NOT NULL,
    src_node_id     INT NOT NULL,
    dst_node_id     INT NOT NULL,
    relation        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (observation_id, src_node_id, dst_node_id, relation),
    CONSTRAINT observation_edge_projections_observation_workspace_fk
        FOREIGN KEY (observation_id, workspace_id)
        REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT observation_edge_projections_src_workspace_fk
        FOREIGN KEY (src_node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE,
    CONSTRAINT observation_edge_projections_dst_workspace_fk
        FOREIGN KEY (dst_node_id, workspace_id)
        REFERENCES memory_nodes(id, workspace_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS observation_edge_projections_edge_idx
    ON observation_edge_projections(src_node_id, dst_node_id, relation);
