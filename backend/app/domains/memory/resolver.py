"""Durable, reviewable identity-resolution candidates."""
import asyncpg

async def record_merge_candidate(conn: asyncpg.Connection, workspace_id: int, survivor_node_id: int,
                                 retired_node_id: int, similarity: float) -> int:
    return await conn.fetchval(
        "INSERT INTO resolution_ledger(workspace_id,survivor_node_id,retired_node_id,similarity) "
        "VALUES($1,$2,$3,$4) ON CONFLICT (survivor_node_id,retired_node_id) "
        "DO UPDATE SET similarity=EXCLUDED.similarity RETURNING id",
        workspace_id, survivor_node_id, retired_node_id, similarity,
    )
