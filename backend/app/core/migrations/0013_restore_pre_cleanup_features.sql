CREATE TABLE IF NOT EXISTS workspace_invites (
    id SERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    email TEXT, created_by INT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ, accepted_by INT REFERENCES users(id) ON DELETE SET NULL, revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS workspace_invites_workspace_idx ON workspace_invites(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS activity_days (
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day DATE NOT NULL, PRIMARY KEY (workspace_id, user_id, day)
);
CREATE INDEX IF NOT EXISTS activity_days_workspace_idx ON activity_days(workspace_id, day);

CREATE TABLE IF NOT EXISTS digests (
    id SERIAL PRIMARY KEY, workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
    channels JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS digests_workspace_idx ON digests(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS decision_shares (
    id SERIAL PRIMARY KEY, workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    node_id INT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE, token TEXT NOT NULL UNIQUE,
    created_by INT REFERENCES users(id) ON DELETE SET NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ, view_count INT NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS decision_shares_active_node_idx ON decision_shares(node_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS answer_feedback (
    id SERIAL PRIMARY KEY, workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    chat_session_id INT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    chat_message_id INT NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    reporter_user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cited_chunk_id INT REFERENCES chunks(id) ON DELETE SET NULL,
    issue_type TEXT NOT NULL CHECK (issue_type IN ('helpful', 'wrong', 'missing_citation', 'bad_citation', 'outdated', 'not_in_memory', 'other')),
    status TEXT NOT NULL CHECK (status IN ('open', 'in_review', 'resolved', 'dismissed')),
    note TEXT, resolution_note TEXT, resolved_by INT REFERENCES users(id) ON DELETE SET NULL,
    resolved_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, chat_message_id, reporter_user_id)
);
CREATE INDEX IF NOT EXISTS answer_feedback_workspace_status_idx ON answer_feedback(workspace_id, status, issue_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS answer_feedback_reporter_idx ON answer_feedback(workspace_id, reporter_user_id, chat_message_id);
