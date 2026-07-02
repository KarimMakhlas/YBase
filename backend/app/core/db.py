import asyncio
import json
from typing import Optional

import asyncpg

from . import config

_pool: Optional[asyncpg.Pool] = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        last_err: Optional[Exception] = None
        for _ in range(30):
            try:
                _pool = await asyncpg.create_pool(
                    config.DATABASE_URL,
                    min_size=config.DB_POOL_MIN_SIZE,
                    max_size=config.DB_POOL_MAX_SIZE,
                    # Per-statement ceiling so a single stuck query can't hold a
                    # pooled connection forever and starve the pool. 0 disables.
                    command_timeout=config.DB_COMMAND_TIMEOUT_S or None,
                    # Neon (and any PgBouncer in transaction mode) does not keep
                    # server-side prepared statements across a pooled connection,
                    # so asyncpg's prepared-statement cache must be off or queries
                    # fail with "prepared statement does not exist". Harmless on a
                    # direct Postgres connection.
                    statement_cache_size=0,
                    init=_init_conn,
                )
                break
            except Exception as e:  # DB may still be starting up
                last_err = e
                await asyncio.sleep(1)
        if _pool is None:
            raise RuntimeError(f"could not connect to Postgres: {last_err}")
    return _pool




async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
