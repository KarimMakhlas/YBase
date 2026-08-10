"""Durable accepted-revision materialization lifecycle contracts."""

from app.domains.documents.ingestion import (
    IngestRequest,
    accept_revision,
    claim_materialization,
    materialize_claimed_revision,
    ingest_document,
)
from app.core import config


def _request(title="Materialize"):
    return IngestRequest(source="meeting", title=title, text="Durable materialization evidence.")


async def test_accepted_revision_has_no_chunks_until_one_materialization_claim(pool, workspace_id):
    document_id, revision_id, duplicate = await accept_revision(_request(), workspace_id)
    assert duplicate is False

    async with pool.acquire() as conn:
        revision_status = await conn.fetchval(
            "SELECT status FROM document_revisions WHERE id=$1", revision_id
        )
        chunks = await conn.fetchval("SELECT count(*) FROM chunks WHERE document_id=$1", document_id)

    first = await claim_materialization()
    second = await claim_materialization()
    async with pool.acquire() as conn:
        claimed_status = await conn.fetchval(
            "SELECT status FROM document_revisions WHERE id=$1", revision_id
        )

    assert revision_status == "accepted"
    assert chunks == 0
    assert first.document_id == document_id
    assert first.revision_id == revision_id
    assert second is None
    assert claimed_status == "materializing"


async def test_claimed_revision_becomes_searchable_before_formation_queue(pool, workspace_id):
    document_id, revision_id, _ = await accept_revision(_request("Searchable"), workspace_id)
    claimed = await claim_materialization()
    assert claimed.document_id == document_id
    assert claimed.revision_id == revision_id

    await materialize_claimed_revision(claimed)

    async with pool.acquire() as conn:
        revision_status = await conn.fetchval(
            "SELECT status FROM document_revisions WHERE id=$1", revision_id
        )
        formation_status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", document_id
        )
        chunk_count = await conn.fetchval("SELECT count(*) FROM chunks WHERE document_id=$1", document_id)

    assert revision_status == "searchable"
    assert formation_status == "pending"
    assert chunk_count == 1


async def test_production_ingest_returns_after_durable_acceptance(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "INGEST_INLINE_MATERIALIZATION", False)
    woken = []

    async def wake():
        woken.append(True)

    monkeypatch.setattr("app.domains.documents.ingestion.worker.wake_preprocessing", wake)
    document_id, duplicate = await ingest_document(_request("Async acceptance"), workspace_id)

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM document_revisions r JOIN documents d ON d.revision_id=r.id "
            "WHERE d.id=$1", document_id,
        )
        chunks = await conn.fetchval("SELECT count(*) FROM chunks WHERE document_id=$1", document_id)

    assert duplicate is False
    assert status == "accepted"
    assert chunks == 0
    assert woken == [True]


async def test_worker_materialization_retries_provider_failure(pool, workspace_id, monkeypatch):
    async def fail_embedding(_texts):
        raise RuntimeError("temporary embedding outage")

    monkeypatch.setattr("app.domains.documents.ingestion.embed_texts", fail_embedding)
    document_id, revision_id, _ = await accept_revision(_request("Retry"), workspace_id)
    claimed = await claim_materialization()

    assert await materialize_claimed_revision(claimed) is False

    async with pool.acquire() as conn:
        revision = await conn.fetchrow(
            "SELECT status, materialization_attempts, materialization_next_attempt_at, error "
            "FROM document_revisions WHERE id=$1", revision_id,
        )
        formation_status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", document_id
        )

    assert revision["status"] == "accepted"
    assert revision["materialization_attempts"] == 1
    assert revision["materialization_next_attempt_at"] is not None
    assert "temporary embedding outage" in revision["error"]
    assert formation_status == "materializing"


async def test_worker_materialization_fails_only_after_retry_budget(pool, workspace_id, monkeypatch):
    async def fail_embedding(_texts):
        raise RuntimeError("persistent embedding outage")

    monkeypatch.setattr("app.domains.documents.ingestion.embed_texts", fail_embedding)
    monkeypatch.setattr(config, "PREPROCESS_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(config, "PREPROCESS_BACKOFF_S", 0)
    document_id, revision_id, _ = await accept_revision(_request("Retry limit"), workspace_id)

    assert await materialize_claimed_revision(await claim_materialization()) is False
    assert await materialize_claimed_revision(await claim_materialization()) is False

    async with pool.acquire() as conn:
        revision = await conn.fetchrow(
            "SELECT status, materialization_attempts FROM document_revisions WHERE id=$1", revision_id
        )
        formation_status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", document_id
        )

    assert revision["status"] == "failed"
    assert revision["materialization_attempts"] == 2
    assert formation_status == "failed"
