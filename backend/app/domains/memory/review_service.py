"""Admin curation APIs for extracted memory nodes."""

from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import db
from app.domains.auth import service as auth

router = APIRouter(prefix="/api/memory-review", tags=["memory-review"])

KINDS = {"decision", "question", "entity", "topic"}
STATES = {"needs_review", "reviewed", "archived", "all"}
DECISION_STATUSES = {"decided", "proposed", "revisited", "reversed", "reaffirmed"}
QUESTION_STATUSES = {"open", "resolved"}


class MemoryPatch(BaseModel):
    label: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    mark_reviewed: bool = False


class ArchiveRequest(BaseModel):
    reason: Optional[str] = None


def _clean_label(label: str) -> str:
    cleaned = " ".join((label or "").split())[:300]
    if not cleaned:
        raise HTTPException(400, "label is required")
    return cleaned


def _clean_status(kind: str, status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    status = status.strip()
    if not status:
        return None
    if kind == "decision" and status in DECISION_STATUSES:
        return status
    if kind == "question" and status in QUESTION_STATUSES:
        return status
    if kind in {"entity", "topic"}:
        raise HTTPException(400, f"{kind} nodes do not use status in v1")
    raise HTTPException(400, f"invalid {kind} status")


async def _node(conn, workspace_id: int, node_id: int):
    row = await conn.fetchrow(
        "SELECT n.*, cu.email AS curated_by_email, au.email AS archived_by_email "
        "FROM memory_nodes n "
        "LEFT JOIN users cu ON cu.id=n.curated_by "
        "LEFT JOIN users au ON au.id=n.archived_by "
        "WHERE n.id=$1 AND n.workspace_id=$2",
        node_id, workspace_id,
    )
    if row is None:
        raise HTTPException(404, "memory node not found")
    return row


async def _detail(conn, workspace_id: int, node_id: int) -> Dict[str, Any]:
    row = await _node(conn, workspace_id, node_id)
    sources = await conn.fetch(
        "SELECT d.id AS document_id, d.source, d.title, d.author, d.doc_created_at, "
        "       c.id AS chunk_id, c.chunk_index, left(c.text, 420) AS snippet "
        "FROM chunk_links cl "
        "JOIN chunks c ON c.id=cl.chunk_id "
        "JOIN documents d ON d.id=c.document_id "
        "WHERE cl.node_id=$1 AND d.workspace_id=$2 "
        "ORDER BY d.doc_created_at NULLS LAST, c.chunk_index LIMIT 20",
        node_id, workspace_id,
    )
    neighbors = await conn.fetch(
        "SELECT e.relation, e.src, e.dst, n.id, n.kind, n.label, n.status, n.archived_at "
        "FROM memory_edges e "
        "JOIN memory_nodes n ON n.id = CASE WHEN e.src=$1 THEN e.dst ELSE e.src END "
        "WHERE e.workspace_id=$2 AND (e.src=$1 OR e.dst=$1) "
        "ORDER BY n.archived_at NULLS FIRST, n.kind, n.label LIMIT 50",
        node_id, workspace_id,
    )
    out = dict(row)
    out["sources"] = [dict(s) for s in sources]
    out["neighbors"] = [
        {
            "node_id": n["id"],
            "kind": n["kind"],
            "label": n["label"],
            "status": n["status"],
            "relation": n["relation"],
            "direction": "out" if n["src"] == node_id else "in",
            "archived": n["archived_at"] is not None,
        }
        for n in neighbors
    ]
    return out


@router.get("")
async def list_review_nodes(
    kind: Optional[str] = None,
    state: str = "needs_review",
    q: Optional[str] = None,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    state = state or "needs_review"
    if state not in STATES:
        raise HTTPException(400, "state must be needs_review, reviewed, archived, or all")
    if kind and kind not in KINDS:
        raise HTTPException(400, "kind must be decision, question, entity, or topic")

    args: List[Any] = [current.workspace_id]
    where = ["n.workspace_id=$1"]
    if kind:
        args.append(kind)
        where.append(f"n.kind=${len(args)}")
    if state == "needs_review":
        where.append("n.curated_at IS NULL AND n.archived_at IS NULL")
    elif state == "reviewed":
        where.append("n.curated_at IS NOT NULL AND n.archived_at IS NULL")
    elif state == "archived":
        where.append("n.archived_at IS NOT NULL")
    if q and q.strip():
        args.append(f"%{q.strip()}%")
        where.append(f"(n.label ILIKE ${len(args)} OR n.summary ILIKE ${len(args)})")

    sql = (
        "SELECT n.id, n.kind, n.label, n.summary, n.status, n.data, n.created_at, n.updated_at, "
        "       n.curated_at, n.curated_by, n.archived_at, n.archived_by, n.archive_reason, "
        "       (SELECT count(*) FROM chunk_links cl WHERE cl.node_id=n.id) AS evidence_count, "
        "       (SELECT count(*) FROM memory_edges e WHERE e.workspace_id=n.workspace_id "
        "        AND (e.src=n.id OR e.dst=n.id)) AS neighbor_count "
        "FROM memory_nodes n WHERE "
        + " AND ".join(where)
        + " ORDER BY n.archived_at NULLS FIRST, n.curated_at NULLS FIRST, "
        "n.updated_at DESC LIMIT 200"
    )
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


@router.get("/{node_id}")
async def get_review_node(
    node_id: int,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return await _detail(conn, current.workspace_id, node_id)


@router.patch("/{node_id}")
async def patch_review_node(
    node_id: int,
    req: MemoryPatch,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _node(conn, current.workspace_id, node_id)
            assignments: List[str] = []
            args: List[Any] = [node_id, current.workspace_id]
            audit_data: Dict[str, Any] = {}

            if "label" in req.model_fields_set:
                label = _clean_label(req.label or "")
                if row["archived_at"] is None:
                    duplicate = await conn.fetchval(
                        "SELECT id FROM memory_nodes "
                        "WHERE workspace_id=$1 AND kind=$2 AND lower(label)=lower($3) "
                        "AND archived_at IS NULL AND id<>$4",
                        current.workspace_id, row["kind"], label, node_id,
                    )
                    if duplicate:
                        raise HTTPException(409, "another active memory node already uses this label")
                args.append(label)
                assignments.append(f"label=${len(args)}")
                audit_data["label"] = label

            if "summary" in req.model_fields_set:
                summary = req.summary.strip() if req.summary else None
                args.append(summary)
                assignments.append(f"summary=${len(args)}")
                audit_data["summary_changed"] = True

            if "status" in req.model_fields_set:
                status = _clean_status(row["kind"], req.status)
                args.append(status)
                assignments.append(f"status=${len(args)}")
                audit_data["status"] = status

            if "data" in req.model_fields_set:
                args.append(req.data or {})
                assignments.append(f"data=${len(args)}")
                audit_data["data_changed"] = True

            should_review = req.mark_reviewed or bool(assignments)
            if should_review:
                args.append(current.user_id)
                assignments.append("curated_at=now()")
                assignments.append(f"curated_by=${len(args)}")
                audit_data["mark_reviewed"] = True

            if assignments:
                try:
                    await conn.fetchrow(
                        "UPDATE memory_nodes SET "
                        + ", ".join(assignments)
                        + ", updated_at=now() WHERE id=$1 AND workspace_id=$2 RETURNING id",
                        *args,
                    )
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, "another active memory node already uses this label")
                action = "memory_edit" if len(audit_data) > 1 or not req.mark_reviewed else "memory_mark_reviewed"
                await auth.audit(conn, action, current.workspace_id, current.user_id,
                                 "memory_node", node_id, audit_data)
            return await _detail(conn, current.workspace_id, node_id)


@router.post("/{node_id}/archive")
async def archive_review_node(
    node_id: int,
    req: ArchiveRequest,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    reason = (req.reason or "").strip()[:500] or None
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _node(conn, current.workspace_id, node_id)
            await conn.execute(
                "UPDATE memory_nodes SET archived_at=COALESCE(archived_at, now()), "
                "archived_by=COALESCE(archived_by, $3), archive_reason=$4, updated_at=now() "
                "WHERE id=$1 AND workspace_id=$2",
                node_id, current.workspace_id, current.user_id, reason,
            )
            await auth.audit(conn, "memory_archive", current.workspace_id, current.user_id,
                             "memory_node", node_id, {"reason": reason})
            return await _detail(conn, current.workspace_id, node_id)


@router.post("/{node_id}/unarchive")
async def unarchive_review_node(
    node_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _node(conn, current.workspace_id, node_id)
            duplicate = await conn.fetchval(
                "SELECT id FROM memory_nodes "
                "WHERE workspace_id=$1 AND kind=$2 AND lower(label)=lower($3) "
                "AND archived_at IS NULL AND id<>$4",
                current.workspace_id, row["kind"], row["label"], node_id,
            )
            if duplicate:
                raise HTTPException(409, "another active memory node already uses this label")
            try:
                await conn.execute(
                    "UPDATE memory_nodes SET archived_at=NULL, archived_by=NULL, "
                    "archive_reason=NULL, updated_at=now() WHERE id=$1 AND workspace_id=$2",
                    node_id, current.workspace_id,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "another active memory node already uses this label")
            await auth.audit(conn, "memory_unarchive", current.workspace_id, current.user_id,
                             "memory_node", node_id)
            return await _detail(conn, current.workspace_id, node_id)
