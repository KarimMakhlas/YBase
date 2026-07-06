-- Multi-instance queue support. formation_claimed_at records when a worker
-- claimed the document, so a leader-elected janitor can requeue documents
-- stranded in 'processing' by a crashed instance without waiting for a
-- restart. The partial index covers the queue's hot scans: claim-candidate
-- selection, janitor requeues, and daily quota resets ('rate_limited' docs
-- returning to 'pending').
ALTER TABLE documents ADD COLUMN IF NOT EXISTS formation_claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS documents_formation_status_idx
    ON documents(formation_status, formation_next_attempt_at)
    WHERE formation_status IN ('pending', 'processing', 'rate_limited');
