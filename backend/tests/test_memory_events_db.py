"""Chronological memory-event contracts."""

from app.domains.memory import events, graph


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
