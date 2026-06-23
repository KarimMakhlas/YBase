# Database migrations

Forward-only SQL migrations, applied by `app/core/migrate.py` on startup.

## How it works

- `../schema.sql` is the **baseline** (version `0000_baseline`) — the full schema
  for a brand-new database. It runs once on a fresh DB and is then recorded.
- Every change to an **existing** schema is a new file here, named
  `NNNN_short_description.sql` (zero-padded, next integer). They run in filename
  order, once each, inside a transaction, tracked in the `schema_migrations` table.

## Adding a migration

1. Create `NNNN_what_it_does.sql` (e.g. `0001_chunks_embed_model.sql`).
2. Write plain SQL. Prefer idempotent forms (`ADD COLUMN IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`) so a retried run is safe.
3. Do **not** edit `schema.sql` for the change — it won't reach existing DBs.
   (You may update `schema.sql` too if you want fresh installs to skip the
   migration, but the migration file is what makes the change land everywhere.)
4. Deploy. The runner applies it automatically; `migrate.run()` logs each one.

## Rules

- Never edit or renumber a migration that has already shipped — append a new one.
- One logical change per file. Keep destructive steps (drops, backfills,
  constraint changes) explicit and reviewed.
- No down-migrations. To undo, write a new forward migration.
