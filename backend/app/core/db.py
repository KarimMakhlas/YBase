import asyncio
import json
from pathlib import Path
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
                    config.DATABASE_URL, min_size=1, max_size=8, init=_init_conn
                )
                break
            except Exception as e:  # DB may still be starting up
                last_err = e
                await asyncio.sleep(1)
        if _pool is None:
            raise RuntimeError(f"could not connect to Postgres: {last_err}")
    return _pool


async def init_schema() -> None:
    pool = await get_pool()
    sql = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(sql)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
