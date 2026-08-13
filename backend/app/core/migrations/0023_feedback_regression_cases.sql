-- Confirmed answer failures become durable, workspace-scoped evaluation cases.
CREATE TABLE IF NOT EXISTS feedback_regression_cases (
    id BIGSERIAL PRIMARY KEY,
    workspace_id INT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    feedback_id INT NOT NULL UNIQUE REFERENCES answer_feedback(id) ON DELETE RESTRICT,
    question TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    expected_citation_chunk_id INT,
    answer_snapshot TEXT NOT NULL,
    created_by_user_id INT REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (expected_citation_chunk_id, workspace_id)
        REFERENCES chunks(id, workspace_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS feedback_regression_cases_workspace_idx
    ON feedback_regression_cases(workspace_id, created_at DESC);

CREATE OR REPLACE FUNCTION prevent_feedback_regression_case_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'feedback regression cases are immutable';
END;
$$;
DROP TRIGGER IF EXISTS feedback_regression_cases_immutable ON feedback_regression_cases;
CREATE TRIGGER feedback_regression_cases_immutable
    BEFORE UPDATE OR DELETE ON feedback_regression_cases
    FOR EACH ROW EXECUTE FUNCTION prevent_feedback_regression_case_mutation();
