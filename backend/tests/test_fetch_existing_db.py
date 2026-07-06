"""Existing-memory selection: the topic-neighbor slice must surface an old
same-topic decision that neither recency (crowded out of the 150 newest) nor
embedding similarity (no stored vector) would find — the unlinked-revisit
failure mode."""

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import formation, graph


async def test_topic_neighbors_surface_stale_decision(pool, workspace_id):
    async with pool.acquire() as conn:
        old_decision = await graph.upsert_node(
            conn, workspace_id, "decision", "Use MySQL for the main database",
            summary="Chosen back in 2020.", status="decided")
        topic = await graph.upsert_node(conn, workspace_id, "topic", "database")
        await graph.add_edge(conn, workspace_id, old_decision, topic, "about")
        # Age the decision out of the recency window: 160 fresher filler nodes
        # (cap is 150), and no stored embedding so relevance can't find it.
        for i in range(160):
            await graph.upsert_node(conn, workspace_id, "entity", f"Filler Person {i}")
        await conn.execute(
            "UPDATE memory_nodes SET updated_at = '2020-01-01' WHERE id=$1",
            old_decision)

    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="DB revisit",
                      text="We are reconsidering our database choice.",
                      tags=["database"]),
        workspace_id=workspace_id,
    )

    async with pool.acquire() as conn:
        existing = await formation._fetch_existing(
            conn, workspace_id, doc_id, doc_tags=["database"])
        # sanity: without the topic slice the decision is not in the recency set
        recent_ids = {r["id"] for r in await conn.fetch(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 "
            "AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 150",
            workspace_id)}
    ids = {r["id"] for r in existing}
    assert old_decision not in recent_ids  # would have been missed before
    assert old_decision in ids             # topic slice rescued it
    assert len(ids) <= 150


async def test_no_tags_no_topic_slice_still_works(pool, workspace_id):
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Plain doc", text="Nothing special."),
        workspace_id=workspace_id,
    )
    async with pool.acquire() as conn:
        existing = await formation._fetch_existing(
            conn, workspace_id, doc_id, doc_tags=[])
    assert isinstance(existing, list)  # empty workspace → empty digest is fine
