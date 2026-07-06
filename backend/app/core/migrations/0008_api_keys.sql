-- Workspace-scoped API keys for the machine-facing agent API (/api/agent/*).
-- Agents can't do cookie login flows, so admins mint long-lived bearer tokens
-- here (shown in plaintext exactly once; only the SHA-256 hash is stored).
-- token_prefix keeps the first characters for display so a key can be
-- recognized in a list without ever storing the secret. Revocation is a
-- timestamp, not a delete — the row stays for audit.
CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    created_by   INT REFERENCES users(id) ON DELETE SET NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_workspace_idx ON api_keys(workspace_id);
