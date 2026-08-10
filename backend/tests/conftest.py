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
# Coordination (Redis) is exercised only by the dedicated tests, which point
# config.REDIS_URL at localhost:6380 themselves and skip when it's down; the
# rest of the suite runs the single-instance (no-Redis) path deterministically.
os.environ["REDIS_URL"] = ""

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


@pytest.fixture(autouse=True)
def _reset_worker_success_marker():
    # module-global last-success timestamp leaks across tests otherwise
    from app.domains.memory import worker

    worker._last_success_at = None
    yield


@pytest.fixture
async def pool():
    """Fresh pool per test (avoids event-loop reuse pitfalls) on a clean DB."""
    p = await db.get_pool()
    await migrate.run()
    async with p.acquire() as conn:
        await conn.execute(
            "TRUNCATE feedback_regression_cases, answer_feedback, memory_events, observation_evidence, observation_edge_projections, observation_projections, "
            "memory_observations, document_revisions, source_objects, sync_jobs, source_streams, source_connections, "
            "oauth_states, oauth_login_states, documents, chunks, chunk_links, "
            "memory_nodes, memory_edges, slack_events, chat_sessions, chat_messages, "
            "formation_runs, audit_events, usage_events, consolidation_queue, resolution_ledger "
            "RESTART IDENTITY CASCADE"
        )
        await conn.execute(
            "UPDATE workspaces SET last_formation_served_at=NULL, "
            "last_materialization_served_at=NULL, active_embedding_model_id=NULL"
        )
    yield p
    await db.close_pool()


def make_formation_result(**over):
    """A minimal, schema-valid formation extraction for fake_llm — override
    fields per test (e.g. decisions=[...])."""
    base = {
        "context_summary": "Test document adds one decision.",
        "decisions": [{
            "title": "Use PostgreSQL as the primary database",
            "what": "Chose PostgreSQL over the alternatives.",
            "reasoning": "Strong transactional guarantees and the team knows it well.",
            "status": "decided",
            "made_by": ["Alice Chen"],
            "positions": ["Alice Chen: argued for Postgres"],
            "alternatives_considered": ["MySQL"],
            "topics": ["database"],
            "date": "2026-01-15",
            "evidence_chunk_indexes": [0],
            "revisits_node_id": None,
            "resolves_question_node_id": None,
            "relates_to_node_ids": [],
        }],
        "entities": [],
        "questions": [],
    }
    base.update(over)
    return base


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace llm.structured_call with a canned FORMATION_SCHEMA-shaped
    result so run_formation is testable end-to-end without a provider.
    Returns a holder whose .result can be swapped mid-test and whose .calls
    records (system, user) prompts."""
    from app.providers import llm

    class Holder:
        def __init__(self):
            self.result = make_formation_result()
            self.calls = []

    holder = Holder()

    async def _fake(system, user, schema, **kw):
        holder.calls.append((system, user))
        return holder.result

    monkeypatch.setattr(llm, "structured_call", _fake)
    return holder


@pytest.fixture
async def redis_coord(monkeypatch):
    """Enable Redis coordination against the compose redis (localhost:6380),
    skipping when unreachable — mirrors the real-Postgres test philosophy.
    Flushes this suite's key prefix and resets the lazy client around the test
    so runs are isolated."""
    from app.core import config as cfg
    from app.core import coordination

    monkeypatch.setattr(cfg, "REDIS_URL", "redis://localhost:6380/0")
    monkeypatch.setattr(cfg, "REDIS_KEY_PREFIX", "ybase-test")
    await coordination.close()
    try:
        client = coordination.get_client()
        await client.ping()
    except Exception:
        await coordination.close()
        pytest.skip("redis not reachable on localhost:6380")
    keys = [k async for k in client.scan_iter(match="ybase-test:*")]
    if keys:
        await client.delete(*keys)
    yield coordination
    await coordination.close()


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
