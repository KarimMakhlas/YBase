-- Topic-scoped API keys: NULL = unrestricted (every key minted so far keeps
-- full-workspace read access). When set, the agent holding the key only sees
-- and proposes memory linked ('about' edges) to topics whose lowercased label
-- is in this list — a "billing" agent can't read or write "hiring" decisions.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_topics TEXT[];
