"""Ingestion dedup and the formation job queue's state machine."""

import asyncio

from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker


def _req(**over):
    base = dict(source="meeting", title="Q1 review", text="We decided things.\n\nMore detail here.")
    base.update(over)
    return IngestRequest(**base)


async def test_ingest_creates_doc_chunks_and_queues(pool, workspace_id):
    doc_id, dup = await ingest_document(_req(), workspace_id=workspace_id)
    assert not dup
    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id)
        chunks = await conn.fetchval(
            "SELECT count(*) FROM chunks WHERE document_id=$1", doc_id)
        chunk_workspaces = await conn.fetch(
            "SELECT DISTINCT workspace_id FROM chunks WHERE document_id=$1", doc_id)
    assert status == "pending"
    assert chunks >= 1
    assert {row["workspace_id"] for row in chunk_workspaces} == {workspace_id}


async def test_chunk_workspace_schema_is_enforced(pool):
    async with pool.acquire() as conn:
        nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='chunks' AND column_name='workspace_id'"
        )
        validated = await conn.fetchval(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname='chunks_document_workspace_fk'"
        )
    assert nullable == "NO"
    assert validated is True


async def test_ingest_exact_duplicate_is_skipped(pool, workspace_id):
    a, dup_a = await ingest_document(_req(), workspace_id=workspace_id)
    b, dup_b = await ingest_document(_req(), workspace_id=workspace_id)
    assert (dup_a, dup_b) == (False, True)
    assert a == b
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM documents") == 1


async def test_ingest_different_text_is_not_duplicate(pool, workspace_id):
    a, _ = await ingest_document(_req(), workspace_id=workspace_id)
    b, dup = await ingest_document(_req(text="Entirely different content."),
                                   workspace_id=workspace_id)
    assert not dup and a != b


async def test_worker_claim_and_success_path(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    assert claimed.doc_id == doc_id
    assert claimed.workspace_id == workspace_id
    assert claimed.lock_token  # LOCAL_TOKEN without Redis, real token with
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT formation_status, formation_claimed_at FROM documents WHERE id=$1",
            doc_id)
    assert row["formation_status"] == "processing"
    assert row["formation_claimed_at"] is not None
    # nothing else pending
    assert await worker._claim() is None


async def test_claim_serializes_per_workspace_but_not_across(pool, workspace_id):
    a, _ = await ingest_document(_req(), workspace_id=workspace_id)
    b, _ = await ingest_document(_req(text="Other content."), workspace_id=workspace_id)
    async with pool.acquire() as conn:
        ws2 = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('W2','w2') "
            "ON CONFLICT DO NOTHING RETURNING id"
        ) or await conn.fetchval("SELECT id FROM workspaces WHERE lower(slug)='w2'")
    c, _ = await ingest_document(_req(text="W2 content."), workspace_id=ws2)

    assert (await worker._claim()).doc_id == a
    # same workspace is blocked while `a` is processing; another workspace isn't
    assert (await worker._claim()).doc_id == c
    assert await worker._claim() is None
    # finishing `a` frees its workspace for `b`
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='complete' WHERE id=$1", a)
    assert (await worker._claim()).doc_id == b


async def test_worker_failure_backs_off_then_fails_permanently(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    for attempt in range(1, config.FORMATION_MAX_ATTEMPTS + 1):
        await worker._claim()
        await worker._record_failure(doc_id, f"boom {attempt}")
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT formation_status, formation_attempts, formation_error, "
                "formation_next_attempt_at FROM documents WHERE id=$1", doc_id)
        assert row["formation_attempts"] == attempt
        if attempt < config.FORMATION_MAX_ATTEMPTS:
            assert row["formation_status"] == "pending"
            assert row["formation_next_attempt_at"] is not None  # backoff scheduled
            # not claimable until the backoff elapses
            assert await worker._claim() is None
            async with pool.acquire() as conn:  # fast-forward the clock
                await conn.execute(
                    "UPDATE documents SET formation_next_attempt_at=now() WHERE id=$1", doc_id)
        else:
            assert row["formation_status"] == "failed"
            assert "boom" in row["formation_error"]


async def test_run_one_times_out_hung_formation(pool, workspace_id, monkeypatch):
    """A formation call that hangs must not pin the worker forever: _run_one
    bounds it with FORMATION_TASK_TIMEOUT_S and records a normal failure so the
    document backs off instead of retrying hot."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()  # mark 'processing', as the loop does before _run_one

    async def _hang(_doc_id, _timer=None):
        await asyncio.sleep(30)  # far longer than the timeout below

    # _form_and_consolidate imports run_formation lazily, so patching the module
    # attribute is enough.
    monkeypatch.setattr("app.domains.memory.formation.run_formation", _hang)
    monkeypatch.setattr(config, "FORMATION_TASK_TIMEOUT_S", 0.2)

    await asyncio.wait_for(worker._run_one(doc_id), timeout=5)  # must return, not hang

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT formation_status, formation_attempts, formation_error "
            "FROM documents WHERE id=$1", doc_id)
    assert row["formation_attempts"] == 1
    assert "timed out" in row["formation_error"]
    assert row["formation_status"] == "pending"  # first failure → backoff, not 'failed'


async def test_formation_health_reports_queue(pool, workspace_id):
    await ingest_document(_req(), workspace_id=workspace_id)
    health = await worker.formation_health()
    assert health["pending"] == 1
    assert health["workers"] == 0           # worker loop isn't started in tests
    assert health["stalled"] is False       # never "stalled" without live workers
    assert health["oldest_pending_age_s"] is not None
    assert health["last_success_age_s"] is None
    worker._mark_success()
    assert (await worker.formation_health())["last_success_age_s"] is not None


async def test_formation_health_flags_stalled_queue(pool, workspace_id, monkeypatch):
    """Pending work + a live worker + nothing completing for a long time = stalled."""
    from datetime import datetime, timedelta, timezone

    await ingest_document(_req(), workspace_id=workspace_id)

    async def _idle():
        await asyncio.sleep(30)

    task = asyncio.ensure_future(_idle())  # a worker that exists but isn't finishing
    monkeypatch.setattr(worker, "_tasks", [task])
    monkeypatch.setattr(
        worker, "_last_success_at",
        datetime.now(timezone.utc) - timedelta(seconds=config.FORMATION_STALL_S + 100),
    )
    try:
        health = await worker.formation_health()
        assert health["workers"] == 1
        assert health["stalled"] is True
    finally:
        task.cancel()


async def test_record_failure_on_deleted_doc_is_safe(pool, workspace_id):
    """A doc deleted mid-formation must not crash the worker. Regression: the
    UPDATE ... RETURNING matched zero rows, attempts came back None, and
    `None >= MAX_ATTEMPTS` raised TypeError, killing the worker loop."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM documents WHERE id=$1", doc_id)
    # must return quietly, not raise
    await worker._record_failure(doc_id, "boom after delete")
    # and the queue is still usable afterwards
    assert await worker._claim() is None


async def test_recover_stuck_requeues_processing(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()
    recovered = await worker.recover_stuck()
    assert recovered == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "pending"
    assert (await worker._claim()).doc_id == doc_id


async def test_release_returns_doc_to_pending(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()
    await worker._release(doc_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT formation_status, formation_claimed_at FROM documents WHERE id=$1",
            doc_id)
    assert row["formation_status"] == "pending"
    assert row["formation_claimed_at"] is None


async def test_janitor_requeues_stale_processing(pool, workspace_id):
    """A doc claimed by an instance that later crashed (claim far older than
    the task timeout, no live workspace lock) goes back to pending via the
    janitor — no restart needed."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_claimed_at = now() - interval '2 hours' "
            "WHERE id=$1", doc_id)
    await worker.janitor_tick()
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "pending"


async def test_janitor_leaves_fresh_claims_alone(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()  # formation_claimed_at = now()
    await worker.janitor_tick()
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "processing"


# ── Cross-instance scenarios (require Redis on localhost:6380) ───────────────


async def test_claim_yields_to_sibling_instance_lock(pool, workspace_id, redis_coord):
    """Another instance already forming this workspace (its wslock is held)
    wins the race: our claim is handed back and the doc stays pending."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    foreign = await redis_coord.try_workspace_lock(workspace_id)
    assert foreign and foreign != redis_coord.LOCAL_TOKEN
    try:
        assert await worker._claim() is None
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT formation_status FROM documents WHERE id=$1", doc_id
            ) == "pending"
    finally:
        await redis_coord.release_workspace_lock(workspace_id, foreign)
    # lock released → claim works again
    claimed = await worker._claim()
    assert claimed.doc_id == doc_id
    await redis_coord.release_workspace_lock(workspace_id, claimed.lock_token)


async def test_recover_stuck_skips_locked_workspace(pool, workspace_id, redis_coord):
    """Startup recovery must not requeue a document a live sibling is still
    forming (its workspace lock is held) — only truly stranded docs."""
    doc_a, _ = await ingest_document(_req(), workspace_id=workspace_id)
    async with pool.acquire() as conn:
        ws2 = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('W2','w2') "
            "ON CONFLICT DO NOTHING RETURNING id"
        ) or await conn.fetchval("SELECT id FROM workspaces WHERE lower(slug)='w2'")
    doc_b, _ = await ingest_document(_req(text="W2 content."), workspace_id=ws2)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='processing' "
            "WHERE id = ANY($1::int[])", [doc_a, doc_b])
    held = await redis_coord.try_workspace_lock(workspace_id)  # sibling forms ws1
    try:
        assert await worker.recover_stuck() == 1  # only ws2's doc
        async with pool.acquire() as conn:
            a_status, b_status = [
                await conn.fetchval(
                    "SELECT formation_status FROM documents WHERE id=$1", d)
                for d in (doc_a, doc_b)
            ]
        assert a_status == "processing"  # left alone: sibling holds the lock
        assert b_status == "pending"
    finally:
        await redis_coord.release_workspace_lock(workspace_id, held)
