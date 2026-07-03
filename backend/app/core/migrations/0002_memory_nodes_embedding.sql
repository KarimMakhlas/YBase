-- Store each decision/question node's signature embedding (label + summary
-- head, same space as chunk embeddings) so post-formation consolidation can
-- compare only the nodes a formation touched instead of re-embedding the
-- whole workspace every run, and so formation can pick its existing-memory
-- digest by relevance to the new document instead of pure recency.
-- NULL = not yet embedded; consolidation lazily backfills.
ALTER TABLE memory_nodes ADD COLUMN IF NOT EXISTS embedding vector(512);
CREATE INDEX IF NOT EXISTS memory_nodes_embedding_idx
    ON memory_nodes USING hnsw (embedding vector_cosine_ops);

-- Formation's existing-memory digest orders by (workspace_id, updated_at);
-- without this index that is a per-workspace scan + sort on every formation.
CREATE INDEX IF NOT EXISTS memory_nodes_ws_updated_idx
    ON memory_nodes(workspace_id, updated_at DESC);
