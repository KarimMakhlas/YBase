-- Source-aware chunk metadata makes evidence spans inspectable and lets later
-- parsers/rankers distinguish document shape without reparsing raw content.
ALTER TABLE document_revisions
    ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'text/plain';

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS section_path TEXT[] NOT NULL DEFAULT ARRAY['legacy'],
    ADD COLUMN IF NOT EXISTS source_start INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS source_end INT,
    ADD COLUMN IF NOT EXISTS content_type TEXT NOT NULL DEFAULT 'text/plain',
    ADD COLUMN IF NOT EXISTS token_count INT;

UPDATE chunks
SET source_end = char_length(text),
    token_count = CASE WHEN btrim(text) = '' THEN 0
                       ELSE cardinality(regexp_split_to_array(btrim(text), E'\\s+')) END
WHERE source_end IS NULL OR token_count IS NULL;

ALTER TABLE chunks ALTER COLUMN source_end SET NOT NULL;
ALTER TABLE chunks ALTER COLUMN token_count SET NOT NULL;
