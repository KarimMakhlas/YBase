# Running YBase on Neon Postgres

Neon is the recommended database for YBase: serverless Postgres with pgvector,
autoscaling compute (no fixed-RAM OOM on the DB), instant branching for free
staging copies, and point-in-time recovery (PITR). This guide covers the move
from the current database (Fly Postgres or local) and the durability setup.

## Why Neon for YBase

- **pgvector built in** — the `chunks.embedding vector(512)` column and HNSW
  index work unchanged.
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
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);
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

## Notes

- Keep the dump file from step 2 only until verified, then delete it — it
  contains all customer data.
- Branching gives you free staging: fork production, run the next migration
  against the branch, confirm, then run it against production.
