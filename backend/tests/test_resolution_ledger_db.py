"""Reversible identity-resolution ledger contracts."""

from app.domains.memory import graph, resolver


async def test_resolution_records_a_reversible_merge(pool, workspace_id):
    async with pool.acquire() as conn:
        keep = await graph.upsert_node(conn, workspace_id, "decision", "Use PostgreSQL")
        drop = await graph.upsert_node(conn, workspace_id, "decision", "Use Postgres")
        ledger_id = await resolver.record_merge_candidate(conn, workspace_id, keep, drop, 0.99)
        row = await conn.fetchrow("SELECT survivor_node_id, retired_node_id, status FROM resolution_ledger WHERE id=$1", ledger_id)
    assert (row["survivor_node_id"], row["retired_node_id"], row["status"]) == (keep, drop, "candidate")
