"""Dated canonical-memory events and deterministic current-state derivation."""

import asyncpg
from datetime import datetime, time, timezone


async def record_decision_event(conn: asyncpg.Connection, workspace_id: int, node_id: int,
                                event_type: str, effective_at: str) -> int:
    date = datetime.combine(datetime.fromisoformat(effective_at).date(), time.min, timezone.utc)
    return await conn.fetchval(
        "INSERT INTO memory_events(workspace_id,node_id,event_type,effective_at) "
        "VALUES($1,$2,$3,$4::timestamptz) RETURNING id",
        workspace_id, node_id, event_type, date,
    )


async def derive_node_status(conn: asyncpg.Connection, node_id: int):
    status = await conn.fetchval(
        "SELECT event_type FROM memory_events WHERE node_id=$1 "
        "ORDER BY effective_at DESC, id DESC LIMIT 1", node_id
    )
    if status is not None:
        await conn.execute(
            "UPDATE memory_nodes SET status=$2, updated_at=now() WHERE id=$1 AND curated_at IS NULL",
            node_id, status,
        )
    return status
