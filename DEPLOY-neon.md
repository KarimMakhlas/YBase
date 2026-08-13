# Running YBase on Neon Postgres

Neon is the recommended database for YBase: serverless Postgres with pgvector,
autoscaling compute (no fixed-RAM OOM on the DB), instant branching for free
staging copies, and point-in-time recovery (PITR). This guide covers the move
from the current database (Fly Postgres or local) and the durability setup.

## Why Neon for YBase

- **pgvector built in** — YBase keeps legacy chunk vectors during migration and
  queries the active per-workspace `chunk_embeddings.embedding vector(512)`
  version through HNSW.
- **Autoscaling compute** — the DB scales with formation/query bursts instead of
  being OOM-killed at a fixed size.
- **Branching** — a production branch can be forked for staging/migration tests
  at zero copy cost.
- **PITR** — restore to any moment within the retention window, which matters for
  a product whose entire value is preserved memory.

## 1. Create the database

1. Create a Neon project (region close to the Fly app, e.g. `aws-us-east-2`).
2. In the SQL editor, enable pgvector: `CREATE EXTENSION IF NOT EXISTS vector;`
3. Copy **two** connection strings from the dashboard:
   - the **pooled** one (host contains `-pooler`) — this is what the app uses.
   - the **direct** one — used only for the one-off data migration restore.

## 2. Migrate the data

If this is a fresh install with no data yet, skip to step 3 — the app creates
its schema on first boot.

To move an existing database, use the helper script. Dump from the current DB
and restore into Neon's **direct** URL (pooled endpoints reject some restore
operations):

```bash
# If the source is Fly Postgres, proxy it locally first (separate terminal):
fly proxy 5432 -a <your-pg-app>

SOURCE_URL='postgres://postgres:<pwd>@localhost:5432/<db>' \
TARGET_URL='postgres://<user>:<pwd>@<ep>.neon.tech/neondb?sslmode=require' \
scripts/migrate_to_neon.sh
```

The script dumps, ensures the `vector` extension, restores, and verifies row
counts plus the presence of the HNSW index. Recreate the index manually if the
verification shows it missing:

```sql
CREATE INDEX CONCURRENTLY chunk_embeddings_embedding_idx
  ON chunk_embeddings USING hnsw (embedding vector_cosine_ops);
```

## 3. Point the app at Neon (pooled)

Use the **pooled** connection string. The app already sets
`statement_cache_size=0` (see `backend/app/core/db.py`) so asyncpg works through
Neon's PgBouncer in transaction mode.

```bash
fly secrets set DATABASE_URL='postgres://<user>:<pwd>@<ep>-pooler.neon.tech/neondb?sslmode=require'
fly deploy
```

Confirm: `GET /api/health` returns `{"db": true}`, and a query returns ranked
results (exercises the vector + HNSW path).

### Pool sizing

`DB_POOL_MAX_SIZE` (default 20) is per app instance. Neon's pooled endpoint
multiplexes thousands of client connections, so 20 is safe; if you run multiple
app machines, keep `instances × DB_POOL_MAX_SIZE` below the project's connection
limit (generous on pooled endpoints, but not infinite).

## 4. Durability: PITR + restore drill

1. In the Neon console, set **history retention** to at least 7 days (the PITR
   window). Longer is safer; it trades a little storage cost.
2. **Do one restore drill before launch** — an untested backup is not a backup:
   - Create a branch from a past timestamp ("restore to a point in time").
   - Point a throwaway app instance (or `psql`) at the branch and confirm the
     data is intact.
   - Delete the branch.
3. Document the restore steps where on-call can find them. In a real incident
   the play is: create a branch at the last-good timestamp, verify, then either
   swap `DATABASE_URL` to the branch or copy corrected rows back.

## 5. Compute, vector, and worker operations

Scale Neon compute from measurements: sustained database CPU above 70%, cache
hit rate below 95%, elevated read I/O, pool wait, or p95 vector/full-text query
latency are useful signals. More compute improves concurrent SQL execution,
working-set cache, HNSW queries, and index builds; it does not repair stale
sources, provider latency, low ANN recall, or unordered memory projection.

- Route public traffic only to `RUNTIME_ROLE=api` instances. Run formation,
  connector, and maintenance loops in `RUNTIME_ROLE=worker` instances (or use
  `all` only for local/dev). Size `DB_POOL_MAX_SIZE` across both roles, not per
  role in isolation.
- Build large HNSW indexes with `CREATE INDEX CONCURRENTLY` on a direct Neon
  connection during a planned window. Validate index plans and tenant
  `recall@10 >= 0.95` before changing query budgets.
- Stage embedding versions side-by-side with
  `scripts/reembed.py --workspace <slug> --activate`. Confirm
  `/api/health/details` reports complete coverage; rollback changes the pointer
  with `--rollback-to <model-key>` and never rewrites vectors.
- Consider stable workspace-bucket partitioning only after query plans show it
  reduces tenant-filtered ANN candidate waste. Isolate an exceptional large
  tenant only with measured benefit. Use read replicas for analytics and
  latency-tolerant evaluation only; ordered ingestion, formation, and
  read-after-write retrieval stay on the primary.
- Release gates: run `make ci`, the tenant recall evaluator, and feedback
  regressions. Block a release if recall/citation precision loses more than two
  points or retrieval p95/provider cost rises more than 20% without a recorded
  trade-off.
- Before a compute-tier change, run both `scripts/retrieval_load_profile.py`
  (100k chunks) and `scripts/worker_load_profile.py` (many workspaces with
  active preprocessing and tenant-scoped queries) against a disposable Neon
  staging branch. Save their output with the release evaluation artifact,
  alongside Neon CPU, cache-hit, I/O, connection-pool, and `EXPLAIN ANALYZE`
  observations. Scale only when those measurements identify database pressure.

## Notes

- Keep the dump file from step 2 only until verified, then delete it — it
  contains all customer data.
- Branching gives you free staging: fork production, run the next migration
  against the branch, confirm, then run it against production.
