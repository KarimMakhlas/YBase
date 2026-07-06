-- Per-run extraction-validation report (counts of silently repaired LLM
-- output: invalid cross-refs, empty topics, trivial reasoning, bad evidence
-- indexes). Makes formation quality drift measurable per workspace.
ALTER TABLE formation_runs ADD COLUMN IF NOT EXISTS validation JSONB NOT NULL DEFAULT '{}';
