"""Durable accepted-revision materialization lifecycle contracts."""

from app.domains.documents.ingestion import IngestRequest, accept_revision, claim_materialization


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
