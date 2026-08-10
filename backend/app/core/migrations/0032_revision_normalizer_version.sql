-- Normalization evolves independently of the source artifact. Persist the
-- exact version so historical revisions can be reprocessed reproducibly.
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS normalizer_version TEXT NOT NULL DEFAULT 'plain-text:v1';
