"""Chronological memory-event contracts."""

from app.domains.memory import events, graph
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory.formation import run_formation
from conftest import make_formation_result


async def test_later_effective_event_wins_even_when_inserted_first(pool, workspace_id):
    async with pool.acquire() as conn:
        node_id = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL", status="proposed"
        )
        await events.record_decision_event(
            conn, workspace_id, node_id, "reversed", "2026-06-01"
        )
        await events.record_decision_event(
            conn, workspace_id, node_id, "decided", "2026-01-15"
        )
        status = await events.derive_node_status(conn, node_id)
        projected = await conn.fetchval("SELECT status FROM memory_nodes WHERE id=$1", node_id)

    assert status == "reversed"
    assert projected == "reversed"


async def test_formation_records_a_dated_decision_event(pool, workspace_id, fake_llm):
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Decision", text="Use PostgreSQL."),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        event = await conn.fetchrow("SELECT event_type, effective_at FROM memory_events")
    assert event["event_type"] == make_formation_result()["decisions"][0]["status"]
    assert event["effective_at"].date().isoformat() == "2026-01-15"
