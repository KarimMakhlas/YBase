"""Project active formation observations into the legacy memory graph."""

from typing import List, Optional

import asyncpg


def _label(kind: str, payload: dict) -> Optional[str]:
    keys = {"decision": "title", "entity": "name", "question": "question"}
    value = payload.get(keys[kind])
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _record_candidate_projection(conn: asyncpg.Connection, run_id: int) -> None:
    """Associate a candidate's already-written compatibility graph with it."""
    observations = await conn.fetch(
        "SELECT id, workspace_id, kind, payload FROM memory_observations "
        "WHERE formation_run_id=$1 AND status='valid' ORDER BY ordinal",
        run_id,
    )
    for observation in observations:
        label = _label(observation["kind"], observation["payload"])
        if not label:
            continue
        node_id = await conn.fetchval(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND kind=$2 "
            "AND lower(label)=lower($3) AND archived_at IS NULL",
            observation["workspace_id"], observation["kind"], label,
        )
        if node_id is None:
            continue
        await conn.execute(
            "INSERT INTO observation_projections(workspace_id, observation_id, node_id) "
            "VALUES($1, $2, $3) ON CONFLICT DO NOTHING",
            observation["workspace_id"], observation["id"], node_id,
        )
        edges = await conn.fetch(
            "SELECT src, dst, relation FROM memory_edges "
            "WHERE workspace_id=$1 AND src=$2",
            observation["workspace_id"], node_id,
        )
        for edge in edges:
            await conn.execute(
                "INSERT INTO observation_edge_projections(workspace_id, observation_id, "
                "src_node_id, dst_node_id, relation) VALUES($1, $2, $3, $4, $5) "
                "ON CONFLICT DO NOTHING",
                observation["workspace_id"], observation["id"], edge["src"],
                edge["dst"], edge["relation"],
            )


async def _retire_projection(conn: asyncpg.Connection, old_run_id: int) -> None:
    """Remove only graph facts that no active observation still backs."""
    old_ids = [r["id"] for r in await conn.fetch(
        "UPDATE memory_observations SET status='retired', retired_at=now() "
        "WHERE formation_run_id=$1 AND status='valid' RETURNING id",
        old_run_id,
    )]
    if not old_ids:
        return
    node_ids = [r["node_id"] for r in await conn.fetch(
        "SELECT DISTINCT node_id FROM observation_projections WHERE observation_id = ANY($1::bigint[])",
        old_ids,
    )]
    await conn.execute(
        "DELETE FROM chunk_links cl USING observation_projections op "
        "JOIN observation_evidence oe ON oe.observation_id=op.observation_id "
        "WHERE op.observation_id = ANY($1::bigint[]) AND cl.node_id=op.node_id "
        "AND cl.chunk_id=oe.chunk_id AND NOT EXISTS ("
        "  SELECT 1 FROM observation_projections active_op "
        "  JOIN observation_evidence active_oe ON active_oe.observation_id=active_op.observation_id "
        "  JOIN memory_observations active_obs ON active_obs.id=active_op.observation_id "
        "  JOIN formation_runs active_run ON active_run.id=active_obs.formation_run_id "
        "  WHERE active_obs.status='valid' AND active_run.is_active "
        "  AND active_op.node_id=cl.node_id AND active_oe.chunk_id=cl.chunk_id"
        ")",
        old_ids,
    )
    await conn.execute(
        "DELETE FROM memory_edges e USING observation_edge_projections old_edge "
        "WHERE old_edge.observation_id = ANY($1::bigint[]) AND e.workspace_id=old_edge.workspace_id "
        "AND e.src=old_edge.src_node_id AND e.dst=old_edge.dst_node_id AND e.relation=old_edge.relation "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM observation_edge_projections active_edge "
        "  JOIN memory_observations active_obs ON active_obs.id=active_edge.observation_id "
        "  JOIN formation_runs active_run ON active_run.id=active_obs.formation_run_id "
        "  WHERE active_obs.status='valid' AND active_run.is_active "
        "  AND active_edge.src_node_id=e.src AND active_edge.dst_node_id=e.dst "
        "  AND active_edge.relation=e.relation"
        ")",
        old_ids,
    )
    if node_ids:
        await conn.execute(
            "DELETE FROM memory_nodes n WHERE n.id = ANY($1::int[]) AND n.curated_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM chunk_links cl WHERE cl.node_id=n.id) "
            "AND NOT EXISTS (SELECT 1 FROM memory_edges e WHERE e.src=n.id OR e.dst=n.id) "
            "AND NOT EXISTS (SELECT 1 FROM observation_projections op "
            "  JOIN memory_observations o ON o.id=op.observation_id "
            "  JOIN formation_runs r ON r.id=o.formation_run_id "
            "  WHERE op.node_id=n.id AND o.status='valid' AND r.is_active)",
            node_ids,
        )


async def _rebuild_candidate_nodes(conn: asyncpg.Connection, run_id: int) -> None:
    """Replace mutable compatibility fields with the active candidate's facts."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (op.node_id) op.node_id, o.kind, o.payload "
        "FROM observation_projections op JOIN memory_observations o "
        "ON o.id=op.observation_id JOIN memory_nodes n ON n.id=op.node_id "
        "WHERE o.formation_run_id=$1 AND o.status='valid' AND n.curated_at IS NULL "
        "ORDER BY op.node_id, o.ordinal DESC",
        run_id,
    )
    for row in rows:
        payload = row["payload"]
        if row["kind"] == "decision":
            summary = (payload.get("what") or "").strip()
            if payload.get("reasoning"):
                summary += "\n\nReasoning: " + payload["reasoning"].strip()
            data = {
                "made_by": payload.get("made_by") or [],
                "positions": payload.get("positions") or [],
                "alternatives_considered": payload.get("alternatives_considered") or [],
                "date": payload.get("date"),
            }
            status = payload.get("status")
        elif row["kind"] == "question":
            summary = None
            data = {"resolution": payload.get("resolution"), "raised_by": payload.get("raised_by") or []}
            status = payload.get("status")
        else:
            summary = payload.get("description") or None
            data = {"entity_kind": payload.get("kind")} if payload.get("kind") else {}
            status = None
        await conn.execute(
            "UPDATE memory_nodes SET summary=$2, status=$3, data=$4, updated_at=now() WHERE id=$1",
            row["node_id"], summary, status, {k: v for k, v in data.items() if v not in (None, "", [])},
        )


async def activate_run(conn: asyncpg.Connection, run_id: int) -> None:
    """Atomically replace a revision's active interpretation with a candidate."""
    candidate = await conn.fetchrow(
        "SELECT id, workspace_id, revision_id FROM formation_runs WHERE id=$1 FOR UPDATE",
        run_id,
    )
    if candidate is None:
        raise RuntimeError(f"formation run {run_id} does not exist")
    prior = await conn.fetchrow(
        "SELECT id FROM formation_runs WHERE revision_id=$1 AND is_active FOR UPDATE",
        candidate["revision_id"],
    )
    await _record_candidate_projection(conn, run_id)
    if prior and prior["id"] != run_id:
        await conn.execute(
            "UPDATE formation_runs SET is_active=false, retired_at=now() WHERE id=$1",
            prior["id"],
        )
    await conn.execute(
        "UPDATE formation_runs SET is_active=true, activated_at=now(), retired_at=NULL WHERE id=$1",
        run_id,
    )
    if prior and prior["id"] != run_id:
        await _retire_projection(conn, prior["id"])
    await _rebuild_candidate_nodes(conn, run_id)
