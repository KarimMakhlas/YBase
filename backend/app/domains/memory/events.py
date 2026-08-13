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


async def record_observation_event(
    conn: asyncpg.Connection,
    workspace_id: int,
    node_id: int,
    observation_id: int,
    event_type: str,
    effective_at: datetime,
) -> int:
    """Idempotently project one immutable observation into a dated event."""
    return await conn.fetchval(
        "INSERT INTO memory_events(workspace_id,node_id,observation_id,event_type,effective_at) "
        "VALUES($1,$2,$3,$4,$5) "
        "ON CONFLICT (observation_id,node_id,event_type) WHERE observation_id IS NOT NULL "
        "DO UPDATE SET node_id=EXCLUDED.node_id, effective_at=EXCLUDED.effective_at "
        "RETURNING id",
        workspace_id, node_id, observation_id, event_type, effective_at,
    )


async def derive_node_status(conn: asyncpg.Connection, node_id: int):
    status = await conn.fetchval(
        "SELECT e.event_type FROM memory_events e "
        "JOIN memory_observations o ON o.id=e.observation_id "
        "AND o.workspace_id=e.workspace_id "
        "JOIN formation_runs r ON r.id=o.formation_run_id "
        "AND r.workspace_id=o.workspace_id "
        "WHERE e.node_id=$1 AND o.status='valid' AND r.is_active "
        "ORDER BY e.effective_at DESC, e.id DESC LIMIT 1", node_id
    )
    if status is None:
        status = await conn.fetchval(
            "SELECT event_type FROM memory_events WHERE node_id=$1 "
            "AND observation_id IS NULL ORDER BY effective_at DESC, id DESC LIMIT 1",
            node_id,
        )
    if status is not None:
        await conn.execute(
            "UPDATE memory_nodes SET status=$2, updated_at=now() WHERE id=$1 AND curated_at IS NULL",
            node_id, status,
        )
    return status
