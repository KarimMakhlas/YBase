-- Email verification for password signups.
--
-- Public signup accepted any address without proving ownership, which enabled
-- an account takeover: an attacker registers victim@corp.com with their own
-- password, the real victim later signs in with Google, and the Google
-- auto-link in _google_find_or_create attaches that identity to the attacker's
-- row — leaving the attacker with password access to the victim's account.
-- The auth code now refuses to auto-link onto an *unverified* password account;
-- this migration supplies the state that check reads.

ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS email_verification_tokens_user_idx
    ON email_verification_tokens(user_id);

-- Backfill. Everyone who already has an account keeps working exactly as
-- before — nobody logs in tomorrow to a "verify your email" banner for an
-- account that predates the feature. Only new password signups start
-- unverified. Google accounts are verified by definition: Google asserts
-- email_verified before we ever create the row.
UPDATE users SET email_verified_at = COALESCE(email_verified_at, now())
WHERE email_verified_at IS NULL;
