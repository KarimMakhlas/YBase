"""Test setup: dedicated ybase_test database on the same pgvector
container, deterministic local hash embeddings (no Ollama/Voyage needed)."""

import os

# Must run before any `app.*` import — config reads env at import time.
os.environ["DATABASE_URL"] = (
    "postgresql://ybase:ybase@localhost:5433/ybase_test"
)
os.environ["EMBED_PROVIDER"] = "local"
# Keep signup fast/deterministic — don't kick off background demo seeding.
os.environ["SEED_DEMO_ON_SIGNUP"] = "false"
# Don't let the auth rate limiter trip during the broad suite; the dedicated
# rate-limit test sets a low ceiling on the limiter object itself.
os.environ["AUTH_RATE_PER_MINUTE"] = "1000"

import asyncio  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402

from app.core import db, migrate  # noqa: E402
import app.providers.embeddings as embeddings  # noqa: E402

_ADMIN_URL = "postgresql://ybase:ybase@localhost:5433/postgres"


@pytest.fixture(scope="session", autouse=True)
def _create_test_db():
    async def go():
        conn = await asyncpg.connect(_ADMIN_URL)
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname='ybase_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE ybase_test")
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
    await migrate.run()
    async with p.acquire() as conn:
        await conn.execute(
            "TRUNCATE answer_feedback, sync_jobs, source_streams, source_connections, "
            "oauth_states, oauth_login_states, documents, chunks, chunk_links, "
            "memory_nodes, memory_edges, slack_events, chat_sessions, chat_messages "
            "RESTART IDENTITY CASCADE"
        )
    yield p
    await db.close_pool()


@pytest.fixture
async def workspace_id(pool):
    # Self-contained: create the default workspace if it isn't there yet. The
    # pool fixture's TRUNCATE deliberately spares `workspaces`, so this persists
    # across tests — but the suite must not depend on a row left over from a
    # previous run (that made tests pass only on a reused database).
    async with pool.acquire() as conn:
        ws = await conn.fetchval(
            "SELECT id FROM workspaces WHERE lower(slug)='default'"
        )
        if ws is None:
            ws = await conn.fetchval(
                "INSERT INTO workspaces(name, slug) VALUES('Default', 'default') "
                "RETURNING id"
            )
        return ws
