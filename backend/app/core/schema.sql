CREATE EXTENSION IF NOT EXISTS vector;

-- Team SaaS identity and tenancy.
CREATE TABLE IF NOT EXISTS workspaces (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS workspaces_slug_idx ON workspaces(lower(slug));

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users(lower(email));

CREATE TABLE IF NOT EXISTS workspace_memberships (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX IF NOT EXISTS workspace_memberships_user_idx ON workspace_memberships(user_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id           SERIAL PRIMARY KEY,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    user_agent   TEXT,
    ip           TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id           SERIAL PRIMARY KEY,
    workspace_id INT REFERENCES workspaces(id) ON DELETE SET NULL,
    actor_user_id INT REFERENCES users(id) ON DELETE SET NULL,
    action       TEXT NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    data         JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_events_workspace_idx ON audit_events(workspace_id, created_at DESC);

-- Shareable workspace invites. A teammate joins by presenting the raw token
-- (only its hash is stored). One row per invite; single-use once accepted.
CREATE TABLE IF NOT EXISTS workspace_invites (
    id           SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    email        TEXT,
    created_by   INT REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    accepted_at  TIMESTAMPTZ,
    accepted_by  INT REFERENCES users(id) ON DELETE SET NULL,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS workspace_invites_workspace_idx
    ON workspace_invites(workspace_id, created_at DESC);

-- One row per (user, workspace, UTC day) the user made an authenticated
-- request. Cheap activity signal for DAU/WAU/retention — written at most once
-- per user per day (see auth._record_activity).
CREATE TABLE IF NOT EXISTS activity_days (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day          DATE NOT NULL,
    PRIMARY KEY (workspace_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS activity_days_workspace_idx ON activity_days(workspace_id, day);

CREATE TABLE IF NOT EXISTS auth_login_attempts (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    ip          TEXT,
    success     BOOLEAN NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS auth_login_attempts_lookup_idx
    ON auth_login_attempts(lower(email), ip, attempted_at DESC);

-- External source connectors (Slack first, generic enough for later providers).
CREATE TABLE IF NOT EXISTS source_connections (
    id                    SERIAL PRIMARY KEY,
    workspace_id          INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    provider              TEXT NOT NULL,
    name                  TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'connected',
    external_workspace_id TEXT,
    access_token_enc      TEXT,
    refresh_token_enc     TEXT,                      -- OAuth refresh token (Jira); NULL for Slack
    token_expires_at      TIMESTAMPTZ,               -- access-token expiry; refreshed when past
    bot_user_id           TEXT,
    metadata              JSONB NOT NULL DEFAULT '{}',
    last_sync_at          TIMESTAMPTZ,
    last_error            TEXT,
    created_by            INT REFERENCES users(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, provider, external_workspace_id)
);
CREATE INDEX IF NOT EXISTS source_connections_workspace_idx
    ON source_connections(workspace_id, provider);
-- Added after initial release for Jira OAuth refresh tokens (idempotent).
ALTER TABLE source_connections ADD COLUMN IF NOT EXISTS refresh_token_enc TEXT;
ALTER TABLE source_connections ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS source_streams (
    id                    SERIAL PRIMARY KEY,
    workspace_id          INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    connection_id         INT NOT NULL REFERENCES source_connections(id) ON DELETE CASCADE,
    provider              TEXT NOT NULL,
    external_id           TEXT NOT NULL,
    name                  TEXT NOT NULL,
    selected              BOOLEAN NOT NULL DEFAULT FALSE,
    status                TEXT NOT NULL DEFAULT 'idle',
    metadata              JSONB NOT NULL DEFAULT '{}',
    sync_cursor           JSONB NOT NULL DEFAULT '{}',
    last_synced_at        TIMESTAMPTZ,
    last_error            TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (connection_id, external_id)
);
CREATE INDEX IF NOT EXISTS source_streams_connection_idx
    ON source_streams(connection_id, selected);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id             SERIAL PRIMARY KEY,
    workspace_id   INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    connection_id  INT NOT NULL REFERENCES source_connections(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    kind           TEXT NOT NULL DEFAULT 'backfill',
    state          JSONB NOT NULL DEFAULT '{}',
    stats          JSONB NOT NULL DEFAULT '{}',
    error          TEXT,
    next_retry_at  TIMESTAMPTZ,
    created_by     INT REFERENCES users(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sync_jobs_connection_idx
    ON sync_jobs(connection_id, created_at DESC);

CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    workspace_id  INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    redirect_path TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at   TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_states_workspace_idx ON oauth_states(workspace_id);

INSERT INTO workspaces (name, slug)
SELECT 'Default Workspace', 'default'
WHERE NOT EXISTS (SELECT 1 FROM workspaces WHERE lower(slug) = 'default');

-- Raw ingested documents (simulated Slack / Notion / GitHub / Jira inputs).
CREATE TABLE IF NOT EXISTS documents (
    id               SERIAL PRIMARY KEY,
    workspace_id     INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source           TEXT NOT NULL,             -- slack | notion | github | jira | meeting | other
    title            TEXT NOT NULL,
    author           TEXT,
    doc_created_at   TIMESTAMPTZ,               -- when the content was originally written
    raw_text         TEXT NOT NULL,
    tags             TEXT[] NOT NULL DEFAULT '{}',
    context_summary  TEXT,                      -- written by memory formation
    formation_status TEXT NOT NULL DEFAULT 'pending',  -- pending | processing | complete | failed
    formation_error  TEXT,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Embedded chunks with provenance back to the document.
CREATE TABLE IF NOT EXISTS chunks (
    id          SERIAL PRIMARY KEY,
    document_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(512) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);

-- Hybrid retrieval: full-text search alongside vector similarity. A generated
-- column backfills existing rows at ALTER time and needs no trigger.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS text_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED;
CREATE INDEX IF NOT EXISTS chunks_text_tsv_idx ON chunks USING gin(text_tsv);

-- The memory graph: decisions, entities, open/resolved questions, topics.
CREATE TABLE IF NOT EXISTS memory_nodes (
    id         SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('decision', 'entity', 'question', 'topic')),
    label      TEXT NOT NULL,
    summary    TEXT,
    status     TEXT,            -- decisions: decided/proposed/revisited/reversed/reaffirmed; questions: open/resolved
    data       JSONB NOT NULL DEFAULT '{}',
    curated_at TIMESTAMPTZ,
    curated_by INT REFERENCES users(id) ON DELETE SET NULL,
    archived_at TIMESTAMPTZ,
    archived_by INT REFERENCES users(id) ON DELETE SET NULL,
    archive_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Typed relations between memory nodes (the reasoning structure):
-- decision -[involves]-> entity, decision -[about]-> topic,
-- decision -[revisits|supersedes|relates_to]-> decision,
-- question -[about]-> topic, question -[raised_by]-> entity,
-- decision -[resolves]-> question.
CREATE TABLE IF NOT EXISTS memory_edges (
    id         SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    src        INT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    dst        INT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    relation   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src, dst, relation)
);
CREATE INDEX IF NOT EXISTS memory_edges_src_idx ON memory_edges(src);
CREATE INDEX IF NOT EXISTS memory_edges_dst_idx ON memory_edges(dst);

-- Provenance: which chunks evidence which memory nodes.
CREATE TABLE IF NOT EXISTS chunk_links (
    chunk_id INT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    node_id  INT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'evidence',
    PRIMARY KEY (chunk_id, node_id, relation)
);
CREATE INDEX IF NOT EXISTS chunk_links_node_idx ON chunk_links(node_id);

-- Persisted chat conversations (the "Ask memory" UI).
-- Periodic per-workspace digests (weekly by default): a snapshot of what's new
-- since the last one. Stored for in-app delivery; email is an optional channel.
CREATE TABLE IF NOT EXISTS digests (
    id            SERIAL PRIMARY KEY,
    workspace_id  INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    period_start  TIMESTAMPTZ NOT NULL,
    period_end    TIMESTAMPTZ NOT NULL,
    payload       JSONB NOT NULL,
    channels      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS digests_workspace_idx ON digests(workspace_id, created_at DESC);

-- Public read-only share links for a single decision (growth loop). The raw
-- token is stored — a low-sensitivity capability URL — so the UI can always
-- re-display the link. At most one active (non-revoked) share per decision.
CREATE TABLE IF NOT EXISTS decision_shares (
    id           SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    node_id      INT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    token        TEXT NOT NULL UNIQUE,
    created_by   INT REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    view_count   INT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS decision_shares_active_node_idx
    ON decision_shares(node_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS chat_sessions (
    id         SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id    INT REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         SERIAL PRIMARY KEY,
    session_id INT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    meta       JSONB,            -- assistant: confidence, citations, timeline, related_questions
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_session_idx ON chat_messages(session_id);

-- Trust loop around Ask Memory answers. Members can mark helpful answers or
-- flag issues; admins inspect and resolve the workspace-scoped queue.
CREATE TABLE IF NOT EXISTS answer_feedback (
    id               SERIAL PRIMARY KEY,
    workspace_id     INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    chat_session_id  INT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    chat_message_id  INT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    reporter_user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cited_chunk_id   INT REFERENCES chunks(id) ON DELETE SET NULL,
    issue_type       TEXT NOT NULL CHECK (
        issue_type IN (
            'helpful', 'wrong', 'missing_citation', 'bad_citation',
            'outdated', 'not_in_memory', 'other'
        )
    ),
    status           TEXT NOT NULL CHECK (status IN ('open', 'in_review', 'resolved', 'dismissed')),
    note             TEXT,
    resolution_note  TEXT,
    resolved_by      INT REFERENCES users(id) ON DELETE SET NULL,
    resolved_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, chat_message_id, reporter_user_id)
);
CREATE INDEX IF NOT EXISTS answer_feedback_workspace_status_idx
    ON answer_feedback(workspace_id, status, issue_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS answer_feedback_reporter_idx
    ON answer_feedback(workspace_id, reporter_user_id, chat_message_id);

-- Idempotent migrations for columns added after the initial schema.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS formation_attempts INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS formation_next_attempt_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS documents_content_hash_idx ON documents(content_hash);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_connection_id INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_stream_id INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS external_ref TEXT;
CREATE INDEX IF NOT EXISTS documents_source_ref_idx
    ON documents(workspace_id, source_connection_id, source_stream_id, external_ref);

-- Buffered Slack Events API messages awaiting thread rollup into documents.
CREATE TABLE IF NOT EXISTS slack_events (
    id         SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    channel    TEXT NOT NULL,
    thread_key TEXT NOT NULL,           -- thread_ts, or ts for top-level messages
    ts         TEXT NOT NULL,
    user_id    TEXT,
    text       TEXT NOT NULL,
    event_at   TIMESTAMPTZ NOT NULL,
    consumed   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS slack_events_thread_idx ON slack_events(consumed, channel, thread_key);

-- Idempotent tenancy migrations for databases created before auth/workspaces.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS workspace_id INT;
UPDATE documents
SET workspace_id = (SELECT id FROM workspaces WHERE lower(slug) = 'default' LIMIT 1)
WHERE workspace_id IS NULL;
ALTER TABLE documents ALTER COLUMN workspace_id SET NOT NULL;

ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS workspace_id INT;
UPDATE memory_nodes
SET workspace_id = (SELECT id FROM workspaces WHERE lower(slug) = 'default' LIMIT 1)
WHERE workspace_id IS NULL;
ALTER TABLE memory_nodes ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS curated_at TIMESTAMPTZ;
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS curated_by INT;
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS archived_by INT;
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS archive_reason TEXT;

ALTER TABLE memory_edges ADD COLUMN IF NOT EXISTS workspace_id INT;
UPDATE memory_edges e
SET workspace_id = n.workspace_id
FROM memory_nodes n
WHERE e.src = n.id AND e.workspace_id IS NULL;
UPDATE memory_edges
SET workspace_id = (SELECT id FROM workspaces WHERE lower(slug) = 'default' LIMIT 1)
WHERE workspace_id IS NULL;
ALTER TABLE memory_edges ALTER COLUMN workspace_id SET NOT NULL;

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS workspace_id INT;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_id INT;
UPDATE chat_sessions
SET workspace_id = (SELECT id FROM workspaces WHERE lower(slug) = 'default' LIMIT 1)
WHERE workspace_id IS NULL;
ALTER TABLE chat_sessions ALTER COLUMN workspace_id SET NOT NULL;

ALTER TABLE slack_events ADD COLUMN IF NOT EXISTS workspace_id INT;
ALTER TABLE slack_events ADD COLUMN IF NOT EXISTS source_connection_id INT;
ALTER TABLE slack_events ADD COLUMN IF NOT EXISTS source_stream_id INT;
UPDATE slack_events
SET workspace_id = (SELECT id FROM workspaces WHERE lower(slug) = 'default' LIMIT 1)
WHERE workspace_id IS NULL;
ALTER TABLE slack_events ALTER COLUMN workspace_id SET NOT NULL;

DROP INDEX IF EXISTS memory_nodes_kind_label_idx;
DROP INDEX IF EXISTS memory_nodes_workspace_kind_label_idx;
CREATE UNIQUE INDEX IF NOT EXISTS memory_nodes_workspace_kind_label_active_idx
    ON memory_nodes(workspace_id, kind, lower(label))
    WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS documents_workspace_idx ON documents(workspace_id);
CREATE INDEX IF NOT EXISTS documents_workspace_content_hash_idx
    ON documents(workspace_id, content_hash);
CREATE INDEX IF NOT EXISTS memory_nodes_workspace_idx ON memory_nodes(workspace_id);
CREATE INDEX IF NOT EXISTS memory_nodes_review_idx
    ON memory_nodes(workspace_id, kind, archived_at, curated_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS memory_edges_workspace_idx ON memory_edges(workspace_id);
CREATE INDEX IF NOT EXISTS chat_sessions_workspace_user_idx
    ON chat_sessions(workspace_id, user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS slack_events_workspace_idx ON slack_events(workspace_id);
CREATE INDEX IF NOT EXISTS slack_events_source_idx
    ON slack_events(source_connection_id, source_stream_id, consumed);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'slack_events_channel_ts_key'
    ) THEN
        ALTER TABLE slack_events DROP CONSTRAINT slack_events_channel_ts_key;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS slack_events_workspace_channel_ts_idx
    ON slack_events(workspace_id, channel, ts);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_workspace_fk') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_workspace_fk
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_nodes_workspace_fk') THEN
        ALTER TABLE memory_nodes ADD CONSTRAINT memory_nodes_workspace_fk
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_nodes_curated_by_fk') THEN
        ALTER TABLE memory_nodes ADD CONSTRAINT memory_nodes_curated_by_fk
            FOREIGN KEY (curated_by) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_nodes_archived_by_fk') THEN
        ALTER TABLE memory_nodes ADD CONSTRAINT memory_nodes_archived_by_fk
            FOREIGN KEY (archived_by) REFERENCES users(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'memory_edges_workspace_fk') THEN
        ALTER TABLE memory_edges ADD CONSTRAINT memory_edges_workspace_fk
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_workspace_fk') THEN
        ALTER TABLE chat_sessions ADD CONSTRAINT chat_sessions_workspace_fk
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_user_fk') THEN
        ALTER TABLE chat_sessions ADD CONSTRAINT chat_sessions_user_fk
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'slack_events_workspace_fk') THEN
        ALTER TABLE slack_events ADD CONSTRAINT slack_events_workspace_fk
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_source_connection_fk') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_source_connection_fk
            FOREIGN KEY (source_connection_id) REFERENCES source_connections(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_source_stream_fk') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_source_stream_fk
            FOREIGN KEY (source_stream_id) REFERENCES source_streams(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'slack_events_source_connection_fk') THEN
        ALTER TABLE slack_events ADD CONSTRAINT slack_events_source_connection_fk
            FOREIGN KEY (source_connection_id) REFERENCES source_connections(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'slack_events_source_stream_fk') THEN
        ALTER TABLE slack_events ADD CONSTRAINT slack_events_source_stream_fk
            FOREIGN KEY (source_stream_id) REFERENCES source_streams(id) ON DELETE CASCADE;
    END IF;
END $$;
