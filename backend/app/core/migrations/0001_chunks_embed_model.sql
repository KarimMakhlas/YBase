-- Record which embedding model produced each chunk's vector. Vectors from
-- different models/dimensions are not comparable, so a future provider or
-- dimension switch must re-embed; this column is how the re-embed step finds
-- what to redo and lets the corpus be migrated model-by-model.
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embed_model TEXT;

-- Pre-tracking rows genuinely have an unknown origin model — mark them as such
-- rather than guessing the current one (which would be wrong if the model has
-- since changed). A re-embed treats 'unknown' as "redo to be safe".
UPDATE chunks SET embed_model = 'unknown' WHERE embed_model IS NULL;
