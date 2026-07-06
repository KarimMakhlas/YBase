"""Batch consolidation: the debounce queue, mutual exclusion with formation,
crash recovery via the janitor, and pgvector/pure-python candidate agreement."""

from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import consolidate, graph, worker
from app.providers.embeddings import embed_texts

from conftest import make_formation_result


def _req(**over):
    base = dict(source="meeting", title="Batch test", text="Two similar decisions.")
    base.update(over)
    return IngestRequest(**base)


def _dup_decisions():
    base = make_formation_result()["decisions"][0]
    return [
        {**base, "title": "Use PostgreSQL as primary database"},
        {**base, "title": "Use PostgreSQL as the primary database"},
    ]


async def test_formation_enqueues_instead_of_inline_merge(
    pool, workspace_id, fake_llm, monkeypatch
):
    monkeypatch.setattr(config, "MERGE_SIM_THRESHOLD", 0.5)
    fake_llm.result = make_formation_result(decisions=_dup_decisions())
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='decision'",
            workspace_id)
        row = await conn.fetchrow(
            "SELECT * FROM consolidation_queue WHERE workspace_id=$1", workspace_id)
    assert n == 2  # both still visible — nothing merged inline anymore
    assert row is not None
    assert len(row["touched_ids"]) == 2
    assert row["running_since"] is None


async def test_enqueue_merges_touched_ids(pool, workspace_id):
    await consolidate.enqueue_touched(workspace_id, [3, 1])
    await consolidate.enqueue_touched(workspace_id, [2, 3])
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT touched_ids FROM consolidation_queue WHERE workspace_id=$1",
            workspace_id)
    assert list(row["touched_ids"]) == [1, 2, 3]


async def test_claim_due_honors_debounce_then_max_delay(pool, workspace_id):
    await consolidate.enqueue_touched(workspace_id, [1, 2])
    assert await consolidate.claim_due() is None  # inside the debounce window

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET last_touched_at = now() - interval '10 minutes'")
    job = await consolidate.claim_due()
    assert job == (workspace_id, [1, 2])
    assert await consolidate.claim_due() is None  # running_since claimed it

    # max-delay path: touches keep landing, but the first touch is old
    await consolidate.release(workspace_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET last_touched_at = now(), "
            "first_touched_at = now() - interval '1 hour'")
    assert await consolidate.claim_due() is not None


async def test_processing_doc_blocks_consolidation_claim(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()  # doc → processing
    await consolidate.enqueue_touched(workspace_id, [1])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET last_touched_at = now() - interval '10 minutes'")
    assert await consolidate.claim_due() is None  # formation in flight

    await worker._release(doc_id)
    assert await consolidate.claim_due() is not None


async def test_running_consolidation_blocks_formation_claim(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await consolidate.enqueue_touched(workspace_id, [1])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET running_since = now() WHERE workspace_id=$1",
            workspace_id)
    assert await worker._claim() is None  # consolidation owns the workspace

    await consolidate.finish(workspace_id)
    claimed = await worker._claim()
    assert claimed is not None and claimed.doc_id == doc_id


async def test_end_to_end_batch_merges_duplicates(
    pool, workspace_id, fake_llm, monkeypatch
):
    monkeypatch.setattr(config, "MERGE_SIM_THRESHOLD", 0.5)
    fake_llm.result = make_formation_result(decisions=_dup_decisions())
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)
    async with pool.acquire() as conn:  # make the batch due
        await conn.execute(
            "UPDATE consolidation_queue SET last_touched_at = now() - interval '10 minutes'")

    assert await worker._try_consolidation() is True

    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='decision'",
            workspace_id)
        remaining = await conn.fetchval("SELECT count(*) FROM consolidation_queue")
        merge_audit = await conn.fetchval(
            "SELECT count(*) FROM audit_events "
            "WHERE workspace_id=$1 AND action='consolidation_merge_nodes'", workspace_id)
    assert n == 1                # duplicates merged by the batch run
    assert remaining == 0        # queue row finished
    assert merge_audit == 1


async def test_finish_keeps_row_when_touched_mid_run(pool, workspace_id):
    await consolidate.enqueue_touched(workspace_id, [1])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET last_touched_at = now() - interval '10 minutes'")
    assert await consolidate.claim_due() is not None
    await consolidate.enqueue_touched(workspace_id, [2])  # lands mid-run
    await consolidate.finish(workspace_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT touched_ids, running_since FROM consolidation_queue "
            "WHERE workspace_id=$1", workspace_id)
    assert row is not None  # kept for the next round
    assert row["running_since"] is None
    assert list(row["touched_ids"]) == [1, 2]


async def test_janitor_resets_stale_running(pool, workspace_id):
    await consolidate.enqueue_touched(workspace_id, [1])
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET running_since = now() - interval '1 hour'")
    assert await consolidate.reset_stale_runs() == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT running_since FROM consolidation_queue WHERE workspace_id=$1",
            workspace_id) is None


async def test_pgvector_candidates_agree_with_pure_python(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "MERGE_SIM_THRESHOLD", 0.5)
    async with pool.acquire() as conn:
        a = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL as primary database",
            summary="Postgres for transactions.", status="decided")
        b = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL as the primary database",
            summary="Postgres for transactions again.", status="decided")
        c = await graph.upsert_node(
            conn, workspace_id, "decision", "Adopt Kubernetes for deployments",
            summary="Container orchestration going forward.", status="decided")

    # pure-python reference over the same signatures
    sigs = await embed_texts([
        "Use PostgreSQL as primary database\nPostgres for transactions.",
        "Use PostgreSQL as the primary database\nPostgres for transactions again.",
        "Adopt Kubernetes for deployments\nContainer orchestration going forward.",
    ])
    ref = consolidate.similar_pairs(
        [(a, sigs[0]), (b, sigs[1]), (c, sigs[2])], config.MERGE_SIM_THRESHOLD)
    ref_pairs = {(k, d) for k, d, _ in ref}

    merged = await consolidate.merge_similar_decisions(workspace_id, [a, b, c])
    db_pairs = {(m["kept"], m["dropped"]) for m in merged}
    assert db_pairs == ref_pairs == {(a, b)}  # A/B merge, C untouched
