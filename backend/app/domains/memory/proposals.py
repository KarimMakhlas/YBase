"""Admin approval for MCP memory proposals."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import db
from app.domains.auth import service as auth
from app.domains.memory import graph

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
DECISION_STATUSES = {"decided", "proposed", "revisited", "reversed", "reaffirmed"}
QUESTION_STATUSES = {"open", "resolved"}
PROPOSAL_STATUSES = {"pending", "approved", "rejected", "all"}


class ApproveProposal(BaseModel):
    label: str | None = None
    summary: str | None = None
    status: str | None = None
    note: str | None = None


class RejectProposal(BaseModel):
    note: str | None = None


def _clean_label(label: str) -> str:
    label = " ".join(label.split())[:300]
    if not label:
        raise HTTPException(400, "label is required")
    return label


def _clean_status(kind: str, status: str | None) -> str | None:
    if not status or not status.strip():
        return None
    status = status.strip()
    if kind == "decision" and status in DECISION_STATUSES:
        return status
    if kind == "question" and status in QUESTION_STATUSES:
        return status
    raise HTTPException(400, f"invalid {kind} status")


@router.get("")
async def list_proposals(status: str = "pending", current: auth.AuthContext = Depends(auth.require_role("admin"))):
    if status not in PROPOSAL_STATUSES:
        raise HTTPException(400, "status must be pending, approved, rejected, or all")
    query = (
        "SELECT p.*, k.name AS key_name, existing.id AS existing_node_id "
        "FROM memory_proposals p "
        "LEFT JOIN api_keys k ON k.id=p.api_key_id "
        "LEFT JOIN LATERAL (SELECT id FROM memory_nodes n "
        "WHERE n.workspace_id=p.workspace_id AND n.kind=p.kind "
        "AND lower(n.label)=lower(p.label) AND n.archived_at IS NULL LIMIT 1) existing ON true "
        "WHERE p.workspace_id=$1"
    )
    args: list[Any] = [current.workspace_id]
    if status != "all":
        query += " AND p.status=$2"
        args.append(status)
    query += " ORDER BY p.created_at DESC LIMIT 200"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return [dict(row) for row in await conn.fetch(query, *args)]


async def _pending(conn, workspace_id: int, proposal_id: int):
    proposal = await conn.fetchrow("SELECT * FROM memory_proposals WHERE id=$1 AND workspace_id=$2 FOR UPDATE", proposal_id, workspace_id)
    if proposal is None:
        raise HTTPException(404, "proposal not found")
    if proposal["status"] != "pending":
        raise HTTPException(409, f"proposal already {proposal['status']}")
    return proposal


@router.post("/{proposal_id}/approve")
async def approve_proposal(proposal_id: int, req: ApproveProposal, current: auth.AuthContext = Depends(auth.require_writable_workspace("admin"))):
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        proposal = await _pending(conn, current.workspace_id, proposal_id)
        kind = proposal["kind"]
        label = _clean_label(req.label if req.label is not None else proposal["label"])
        status = _clean_status(kind, req.status or proposal["status_suggestion"]) or ("decided" if kind == "decision" else "open")
        node_id = await graph.upsert_node(conn, current.workspace_id, kind, label, summary=(req.summary if req.summary is not None else proposal["summary"]).strip(), status=status, data={**dict(proposal["data"] or {}), "proposal_id": proposal_id})
        await conn.execute(
            "UPDATE memory_nodes SET curated_at=now(), curated_by=$3, updated_at=now() "
            "WHERE id=$1 AND workspace_id=$2",
            node_id, current.workspace_id, current.user_id,
        )
        for topic in proposal["topics"] or []:
            topic_id = await graph.upsert_node(conn, current.workspace_id, "topic", topic.strip().lower())
            await graph.add_edge(conn, current.workspace_id, node_id, topic_id, "about")
        note = (req.note or "").strip()[:500] or None
        await conn.execute("UPDATE memory_proposals SET status='approved', reviewed_by=$3, reviewed_at=now(), resolution_note=$4, created_node_id=$5 WHERE id=$1 AND workspace_id=$2", proposal_id, current.workspace_id, current.user_id, note, node_id)
        await auth.audit(conn, "proposal_approved", current.workspace_id, current.user_id, "memory_proposal", proposal_id, {"node_id": node_id})
    return {"proposal_id": proposal_id, "status": "approved", "node_id": node_id}


@router.post("/{proposal_id}/reject")
async def reject_proposal(proposal_id: int, req: RejectProposal, current: auth.AuthContext = Depends(auth.require_writable_workspace("admin"))):
    note = (req.note or "").strip()[:500] or None
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _pending(conn, current.workspace_id, proposal_id)
        await conn.execute("UPDATE memory_proposals SET status='rejected', reviewed_by=$3, reviewed_at=now(), resolution_note=$4 WHERE id=$1 AND workspace_id=$2", proposal_id, current.workspace_id, current.user_id, note)
        await auth.audit(conn, "proposal_rejected", current.workspace_id, current.user_id, "memory_proposal", proposal_id, {"note": note})
    return {"proposal_id": proposal_id, "status": "rejected"}
