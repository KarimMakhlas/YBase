-- Completion latency hides slow first response and strict-verification
-- buffering. Keep the user-visible latency separately for release budgets.
ALTER TABLE query_runs
    ADD COLUMN IF NOT EXISTS first_visible_ms INT;
