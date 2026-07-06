"""Lightweight, forward-only SQL migration runner.

The schema is a frozen baseline (`schema.sql`) plus numbered, additive migration
files in `migrations/` (`0001_*.sql`, `0002_*.sql`, ...). Each file is applied
exactly once, in filename order, inside its own transaction, and recorded in the
`schema_migrations` table. There are no down-migrations — to change something,
roll forward with a new numbered file.

Why this exists: before this, every schema change was an `IF NOT EXISTS` line
appended to `schema.sql` that re-ran on every boot, with no record of what had
been applied and no safe way to run a destructive change (rename, backfill,
constraint). The runner gives ordered, tracked, transactional changes.

RULES
- `schema.sql` is the baseline. It only ever runs on a brand-new database.
  Do NOT add new changes to it after launch — they won't apply to existing DBs.
- Every change to an existing schema is a new file: `migrations/NNNN_name.sql`,
  with NNNN the next zero-padded integer. Keep each migration idempotent where
  practical (IF NOT EXISTS / IF EXISTS) so a half-applied run can be retried.
- Migrations run in one transaction each; a failure rolls that file back and
  stops the run (later files are not applied).
"""

import asyncio
import logging
from pathlib import Path
from typing import List, Set, Tuple

from . import db

log = logging.getLogger("whybase.migrate")

_BASELINE_VERSION = "0000_baseline"
_BASELINE_FILE = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _ensure_table(conn) -> Set[str]:
    """Create the tracking table if needed and return the applied versions."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version    TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


def _pending_files() -> List[Tuple[str, Path]]:
    """Numbered migration files sorted by name. The version is the filename
    stem (e.g. '0001_chunks_embed_model')."""
    if not _MIGRATIONS_DIR.is_dir():
        return []
    files = sorted(p for p in _MIGRATIONS_DIR.glob("[0-9]*.sql"))
    return [(p.stem, p) for p in files]


async def run() -> List[str]:
    """Apply the baseline (once) then any unapplied numbered migrations, each in
    its own transaction. Returns the versions applied during this call (empty
    when the database is already up to date). Safe to call on every startup."""
    pool = await db.get_pool()
    applied_now: List[str] = []
    async with pool.acquire() as conn:
        # Two instances booting at once must not race migrations. A polled
        # try-lock (not pg_advisory_lock) keeps each attempt inside the pool's
        # per-statement timeout while the other instance migrates.
        got = False
        for _ in range(120):
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext('ybase_migrate'))"
            )
            if got:
                break
            await asyncio.sleep(1)
        if not got:
            raise RuntimeError("could not acquire migration advisory lock")
        try:
            return await _run_locked(conn, applied_now)
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtext('ybase_migrate'))"
            )


async def _run_locked(conn, applied_now: List[str]) -> List[str]:
    done = await _ensure_table(conn)

    # Baseline = the whole schema.sql. It is all IF NOT EXISTS, so running it
    # on a database that already has the schema (e.g. one created before the
    # runner existed) is a safe no-op; we record it so it never runs again.
    if _BASELINE_VERSION not in done:
        async with conn.transaction():
            await conn.execute(_BASELINE_FILE.read_text())
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES($1)",
                _BASELINE_VERSION,
            )
        applied_now.append(_BASELINE_VERSION)
        log.info("applied baseline schema (%s)", _BASELINE_VERSION)

    for version, path in _pending_files():
        if version in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "INSERT INTO schema_migrations(version) VALUES($1)", version
            )
        applied_now.append(version)
        log.info("applied migration %s", version)

    if not applied_now:
        log.info("database schema up to date")
    return applied_now
