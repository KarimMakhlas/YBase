"""Ingestion dedup and the formation job queue's state machine."""

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
    assert status == "pending"
    assert chunks >= 1


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
    assert claimed == doc_id
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "processing"
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

    assert await worker._claim() == a
    # same workspace is blocked while `a` is processing; another workspace isn't
    assert await worker._claim() == c
    assert await worker._claim() is None
    # finishing `a` frees its workspace for `b`
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='complete' WHERE id=$1", a)
    assert await worker._claim() == b


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
    assert await worker._claim() == doc_id


async def test_release_returns_doc_to_pending(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()
    await worker._release(doc_id)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "pending"
