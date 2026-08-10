"""Immutable source revision history and active-search projection coverage."""

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.query.retrieval import retrieve

from test_api_endpoints import _auth_client


def _req(text):
    return IngestRequest(
        source="meeting",
        title="Policy",
        text=text,
        idempotency_key="policy-source",
    )


async def test_retrieval_excludes_retired_document_revisions(pool, workspace_id):
    first, _ = await ingest_document(_req("obsolete blue policy"), workspace_id)
    second, _ = await ingest_document(_req("current green policy"), workspace_id)

    result = await retrieve("policy", workspace_id)

    assert first != second
    assert any("current green" in chunk["text"] for chunk in result["chunks"])
    assert not any("obsolete blue" in chunk["text"] for chunk in result["chunks"])


async def test_document_listing_exposes_revision_projection_metadata(pool, workspace_id):
    document_id, _ = await ingest_document(_req("current policy"), workspace_id)
    client, _ = await _auth_client(pool, workspace_id, role="admin")

    async with client:
        response = await client.get("/api/documents")

    assert response.status_code == 200
    row = next(document for document in response.json() if document["id"] == document_id)
    assert row["source_object_id"] is not None
    assert row["revision_id"] is not None
    assert row["is_active"] is True


async def test_source_object_deletion_hides_the_current_document(pool, workspace_id):
    from app.domains.documents import ingestion

    document_id, _ = await ingest_document(_req("removed source"), workspace_id)

    assert await ingestion.mark_source_deleted_for_document(document_id, workspace_id)

    async with pool.acquire() as conn:
        document = await conn.fetchrow(
            "SELECT is_active FROM documents WHERE id=$1", document_id
        )
        revision = await conn.fetchrow(
            "SELECT r.status, so.status AS source_status "
            "FROM documents d JOIN document_revisions r ON r.id=d.revision_id "
            "JOIN source_objects so ON so.id=d.source_object_id WHERE d.id=$1",
            document_id,
        )
    assert document["is_active"] is False
    assert revision["status"] == "deleted"
    assert revision["source_status"] == "deleted"
