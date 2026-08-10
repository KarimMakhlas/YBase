"""Durable, reviewable identity-resolution candidates."""
import asyncpg

from app.core import config


_ALLOWED_TRANSITIONS = {
    "candidate": {"approved", "rejected"},
    "approved": {"reverted"},
    "rejected": set(),
    "reverted": set(),
}


async def eligible_for_automatic_resolution(
    conn: asyncpg.Connection,
    workspace_id: int,
    survivor_node_id: int,
    retired_node_id: int,
    similarity: float,
) -> bool:
    """Require high similarity plus active, independent source evidence."""
    if survivor_node_id == retired_node_id or similarity < config.RESOLVER_AUTO_THRESHOLD:
        return False
    node_ids = [survivor_node_id, retired_node_id]
    eligible_nodes = await conn.fetch(
        "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND id = ANY($2::int[]) "
        "AND kind='decision' AND archived_at IS NULL AND curated_at IS NULL",
        workspace_id, node_ids,
    )
    if {row["id"] for row in eligible_nodes} != set(node_ids):
        return False
    evidence_rows = await conn.fetch(
        "SELECT op.node_id, count(DISTINCT o.id) AS observation_count "
        "FROM observation_projections op "
        "JOIN memory_observations o ON o.id=op.observation_id "
        "JOIN formation_runs r ON r.id=o.formation_run_id "
        "JOIN observation_evidence oe ON oe.observation_id=o.id "
        "WHERE op.node_id = ANY($1::int[]) AND o.workspace_id=$2 "
        "AND o.status='valid' AND r.is_active "
        "GROUP BY op.node_id",
        node_ids, workspace_id,
    )
    return {row["node_id"] for row in evidence_rows} == set(node_ids)

async def record_merge_candidate(conn: asyncpg.Connection, workspace_id: int, survivor_node_id: int,
                                 retired_node_id: int, similarity: float) -> int:
    automatic = await eligible_for_automatic_resolution(
        conn, workspace_id, survivor_node_id, retired_node_id, similarity
    )
    status = "approved" if automatic else "candidate"
    previous_status = await conn.fetchval(
        "SELECT status FROM resolution_ledger WHERE survivor_node_id=$1 AND retired_node_id=$2",
        survivor_node_id, retired_node_id,
    )
    ledger_id = await conn.fetchval(
        "INSERT INTO resolution_ledger(workspace_id,survivor_node_id,retired_node_id,similarity,status,evidence,resolved_at) "
        "VALUES($1,$2,$3,$4,$5,jsonb_build_object('automatic', $6::boolean), "
        "CASE WHEN $5='approved' THEN now() ELSE NULL END) "
        "ON CONFLICT (survivor_node_id,retired_node_id) DO UPDATE SET "
        "similarity=EXCLUDED.similarity, evidence=EXCLUDED.evidence, updated_at=now(), "
        "status=CASE WHEN resolution_ledger.status='candidate' THEN EXCLUDED.status "
        "ELSE resolution_ledger.status END, "
        "resolved_at=CASE WHEN resolution_ledger.status='candidate' AND EXCLUDED.status='approved' "
        "THEN now() ELSE resolution_ledger.resolved_at END "
        "RETURNING id",
        workspace_id, survivor_node_id, retired_node_id, similarity, status, automatic,
    )
    if automatic and previous_status in (None, "candidate"):
        from app.domains.auth import service as auth  # lazy: avoid import cycle
        await auth.audit(
            conn, "resolution_candidate_auto_approved", workspace_id, None,
            target_type="resolution_ledger", target_id=ledger_id,
            data={"previous_status": previous_status or "candidate", "similarity": similarity},
        )
    return ledger_id


async def set_candidate_status(
    conn: asyncpg.Connection,
    ledger_id: int,
    workspace_id: int,
    status: str,
    actor_id: int | None = None,
) -> bool:
    """Advance an auditable resolution decision without deleting graph facts."""
    row = await conn.fetchrow(
        "SELECT status FROM resolution_ledger WHERE id=$1 AND workspace_id=$2 FOR UPDATE",
        ledger_id, workspace_id,
    )
    if row is None or status not in _ALLOWED_TRANSITIONS.get(row["status"], set()):
        return False
    await conn.execute(
        "UPDATE resolution_ledger SET status=$3, updated_at=now(), resolved_at=now(), "
        "resolved_by_user_id=$4 WHERE id=$1 AND workspace_id=$2",
        ledger_id, workspace_id, status, actor_id,
    )
    from app.domains.auth import service as auth  # lazy: avoid import cycle
    await auth.audit(
        conn, f"resolution_candidate_{status}", workspace_id, actor_id,
        target_type="resolution_ledger", target_id=ledger_id,
        data={"previous_status": row["status"]},
    )
    return True
