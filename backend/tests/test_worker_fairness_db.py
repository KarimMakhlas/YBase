"""Workspace-fair durable formation claim contracts."""

from uuid import uuid4

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker


def _request(title: str) -> IngestRequest:
    return IngestRequest(source="meeting", title=title, text="Decision content.")


async def test_claim_rotates_between_eligible_workspaces_before_reclaiming_backlog(
    pool, workspace_id
):
    first_a, _ = await ingest_document(_request("A first"), workspace_id=workspace_id)
    second_a, _ = await ingest_document(_request("A second"), workspace_id=workspace_id)
    async with pool.acquire() as conn:
        workspace_b = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('Workspace B', $1) "
            "RETURNING id",
            f"fair-workspace-b-{uuid4().hex}",
        )
    first_b, _ = await ingest_document(_request("B first"), workspace_id=workspace_b)

    first = await worker._claim()
    await worker._release(first.doc_id)
    second = await worker._claim()
    await worker._release(second.doc_id)
    third = await worker._claim()
    await worker._release(third.doc_id)

    assert first.doc_id == first_a
    assert second.doc_id == first_b
    assert third.doc_id == first_a
    assert second_a != first_a
