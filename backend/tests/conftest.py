"""Test setup: dedicated whybase_test database on the same pgvector
container, deterministic local hash embeddings (no Ollama/Voyage needed)."""

import os

# Must run before any `app.*` import — config reads env at import time.
os.environ["DATABASE_URL"] = (
    "postgresql://whybase:whybase@localhost:5433/whybase_test"
)
os.environ["EMBED_PROVIDER"] = "local"

import asyncio  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402

from app.core import db  # noqa: E402
import app.providers.embeddings as embeddings  # noqa: E402

_ADMIN_URL = "postgresql://whybase:whybase@localhost:5433/postgres"


@pytest.fixture(scope="session", autouse=True)
def _create_test_db():
    async def go():
        conn = await asyncpg.connect(_ADMIN_URL)
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='whybase_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE whybase_test")
        await conn.close()

    asyncio.run(go())


@pytest.fixture(autouse=True)
def _pin_local_embeddings():
    embeddings._provider = "local"
    yield
    embeddings._provider = None


@pytest.fixture
async def pool():
    """Fresh pool per test (avoids event-loop reuse pitfalls) on a clean DB."""
    p = await db.get_pool()
    await db.init_schema()
    async with p.acquire() as conn:
        await conn.execute(
            "TRUNCATE answer_feedback, sync_jobs, source_streams, source_connections, "
            "oauth_states, documents, chunks, chunk_links, memory_nodes, memory_edges, "
            "slack_events, chat_sessions, chat_messages RESTART IDENTITY CASCADE"
        )
    yield p
    await db.close_pool()


@pytest.fixture
async def workspace_id(pool):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM workspaces WHERE lower(slug)='default'"
        )
