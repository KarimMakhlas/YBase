#!/usr/bin/env bash
#
# Migrate the YBase Postgres database to Neon (or any managed pgvector Postgres).
#
# Dumps the current database and restores it into the target, preserving the
# pgvector data (embeddings) and the HNSW index. Safe to re-run: pg_restore is
# given --clean --if-exists so a partial run can be retried.
#
# Usage:
#   SOURCE_URL=postgres://user:pass@localhost:5432/whybase \
#   TARGET_URL='postgres://user:pass@ep-xxx.neon.tech/neondb?sslmode=require' \
#   scripts/migrate_to_neon.sh
#
# To dump from a Fly Postgres, first open a local proxy in another terminal:
#   fly proxy 5432 -a <your-pg-app>
# then set SOURCE_URL=postgres://postgres:<pwd>@localhost:5432/<db>
#
# Requires: pg_dump, pg_restore, psql (Postgres 16 client, matching the server).

set -euo pipefail

: "${SOURCE_URL:?set SOURCE_URL to the current database connection string}"
: "${TARGET_URL:?set TARGET_URL to the Neon connection string}"

DUMP_FILE="${DUMP_FILE:-ybase-$(date +%Y%m%d-%H%M%S).dump}"

echo "==> 1/4  Dumping source -> ${DUMP_FILE}"
# -Fc = custom format (compressed, allows selective/parallel restore).
# --no-owner / --no-privileges: the target (Neon) owns objects under its own
# role, so don't carry source role grants that won't exist there.
pg_dump -Fc --no-owner --no-privileges "${SOURCE_URL}" -f "${DUMP_FILE}"

echo "==> 2/4  Ensuring pgvector extension on target"
# Neon allows the 'vector' extension from its catalog; create it before restore
# so the chunks.embedding column type resolves.
psql "${TARGET_URL}" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "==> 3/4  Restoring into target"
# --clean --if-exists makes a retry idempotent. --no-owner because Neon's role
# differs from the source. Errors are surfaced (set -e) but the extension and
# a few comments may already exist; those specific NOTICEs are harmless.
pg_restore --clean --if-exists --no-owner --no-privileges \
  --dbname "${TARGET_URL}" "${DUMP_FILE}" || {
    echo "    pg_restore reported errors — review above. Common harmless cases:"
    echo "    'extension \"vector\" already exists', comment-on-extension perms."
    echo "    Verify the row counts in step 4 before trusting the migration."
  }

echo "==> 4/4  Verifying"
echo "    -- table row counts --"
psql "${TARGET_URL}" -v ON_ERROR_STOP=1 -c \
  "SELECT 'documents' t, count(*) FROM documents
   UNION ALL SELECT 'chunks', count(*) FROM chunks
   UNION ALL SELECT 'memory_nodes', count(*) FROM memory_nodes
   UNION ALL SELECT 'memory_edges', count(*) FROM memory_edges;"
echo "    -- HNSW index on chunks.embedding (must be present) --"
psql "${TARGET_URL}" -v ON_ERROR_STOP=1 -c \
  "SELECT indexname, indexdef FROM pg_indexes
   WHERE tablename='chunks' AND indexdef ILIKE '%hnsw%';"

cat <<EOF

Done. Next steps:
  1. Compare the row counts above against the source.
  2. If the HNSW index row is empty, recreate it:
       CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
  3. Point the app at Neon's POOLED connection string (…-pooler.neon.tech),
     not the direct one — the app sets statement_cache_size=0 for PgBouncer.
       fly secrets set DATABASE_URL='<neon-pooled-url>'
       fly deploy
  4. Enable PITR / history retention in the Neon console (see DEPLOY-neon.md).
  5. Delete ${DUMP_FILE} once verified — it contains all your data.
EOF
