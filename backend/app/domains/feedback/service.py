"""Answer feedback APIs for the Ask Memory trust loop."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import db
from app.domains.auth import service as auth

router = APIRouter(prefix="/api/answer-feedback", tags=["answer-feedback"])

ISSUE_TYPES = {
    "helpful",
    "wrong",
    "missing_citation",
    "bad_citation",
    "outdated",
    "not_in_memory",
    "other",
}
STATUSES = {"open", "in_review", "resolved", "dismissed"}

# Shared row shape for a feedback item joined to its reporter, resolver,
# session, answer message, and (optional) cited chunk + document. Callers
# append their own WHERE/ORDER BY.
_FEEDBACK_SELECT = (
    "SELECT f.*, "
    "       u.display_name AS reporter_name, u.email AS reporter_email, "
    "       ru.display_name AS resolved_by_name, "
    "       s.title AS session_title, "
    "       m.content AS answer_text, m.meta AS message_meta, "
    "       c.chunk_index AS cited_chunk_index, c.text AS cited_chunk_text, "
    "       d.id AS cited_document_id, d.title AS cited_document_title, "
    "       d.source AS cited_document_source, d.author AS cited_document_author "
    "FROM answer_feedback f "
    "JOIN users u ON u.id=f.reporter_user_id "
    "LEFT JOIN users ru ON ru.id=f.resolved_by "
    "JOIN chat_sessions s ON s.id=f.chat_session_id "
    "JOIN chat_messages m ON m.id=f.chat_message_id "
    "LEFT JOIN chunks c ON c.id=f.cited_chunk_id "
    "LEFT JOIN documents d ON d.id=c.document_id "
)


class FeedbackCreate(BaseModel):
    chat_message_id: int
    issue_type: str
    note: Optional[str] = None
    cited_chunk_id: Optional[int] = None


class FeedbackPatch(BaseModel):
    status: Optional[str] = None
    resolution_note: Optional[str] = None


def _clean_issue_type(issue_type: str) -> str:
    cleaned = (issue_type or "").strip()
    if cleaned not in ISSUE_TYPES:
        raise HTTPException(
            400,
            "issue_type must be helpful, wrong, missing_citation, bad_citation, "
            "outdated, not_in_memory, or other",
        )
    return cleaned


def _clean_status(status: str) -> str:
    cleaned = (status or "").strip()
    if cleaned not in STATUSES:
        raise HTTPException(400, "status must be open, in_review, resolved, or dismissed")
    return cleaned


def _clean_note(value: Optional[str]) -> Optional[str]:
    cleaned = (value or "").strip()
    return cleaned[:2000] or None


def _row_to_feedback(row) -> Dict[str, Any]:
    meta = row["message_meta"] or {}
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "chat_session_id": row["chat_session_id"],
        "chat_message_id": row["chat_message_id"],
        "reporter_user_id": row["reporter_user_id"],
        "reporter_name": row["reporter_name"],
        "reporter_email": row["reporter_email"],
        "issue_type": row["issue_type"],
        "status": row["status"],
        "note": row["note"],
        "resolution_note": row["resolution_note"],
        "resolved_by": row["resolved_by"],
        "resolved_by_name": row["resolved_by_name"],
        "resolved_at": row["resolved_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "session_title": row["session_title"],
        "answer_text": row["answer_text"],
        "answer_preview": (row["answer_text"] or "")[:280],
        "message_meta": meta,
        "citations": meta.get("citations") or [],
        "trace": meta.get("trace") or {},
        "cited_chunk": (
            {
                "chunk_id": row["cited_chunk_id"],
                "chunk_index": row["cited_chunk_index"],
                "text": row["cited_chunk_text"],
                "document_id": row["cited_document_id"],
                "document_title": row["cited_document_title"],
                "document_source": row["cited_document_source"],
                "document_author": row["cited_document_author"],
            }
            if row["cited_chunk_id"] is not None
            else None
        ),
    }


async def _ensure_own_assistant_message(
    conn,
    current: auth.AuthContext,
    chat_message_id: int,
) -> Dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT m.id, m.session_id, m.role, s.workspace_id, s.user_id "
        "FROM chat_messages m "
        "JOIN chat_sessions s ON s.id=m.session_id "
        "WHERE m.id=$1 AND s.workspace_id=$2",
        chat_message_id,
        current.workspace_id,
    )
    if row is None:
        raise HTTPException(404, "chat message not found")
    if row["role"] != "assistant":
        raise HTTPException(400, "feedback can only be attached to assistant messages")
    if row["user_id"] != current.user_id:
        raise HTTPException(403, "cannot submit feedback for another user's conversation")
    return dict(row)


async def _ensure_workspace_chunk(conn, workspace_id: int, chunk_id: Optional[int]) -> None:
    if chunk_id is None:
        return
    exists = await conn.fetchval(
        "SELECT 1 FROM chunks c JOIN documents d ON d.id=c.document_id "
        "WHERE c.id=$1 AND d.workspace_id=$2",
        chunk_id,
        workspace_id,
    )
    if not exists:
        raise HTTPException(400, "cited chunk does not belong to this workspace")


async def _detail(conn, workspace_id: int, feedback_id: int) -> Dict[str, Any]:
    row = await conn.fetchrow(
        _FEEDBACK_SELECT + "WHERE f.id=$1 AND f.workspace_id=$2",
        feedback_id,
        workspace_id,
    )
    if row is None:
        raise HTTPException(404, "feedback not found")
    return _row_to_feedback(row)


@router.post("")
async def submit_feedback(
    req: FeedbackCreate,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("member")),
) -> Dict[str, Any]:
    issue_type = _clean_issue_type(req.issue_type)
    note = _clean_note(req.note)
    status = "resolved" if issue_type == "helpful" else "open"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            msg = await _ensure_own_assistant_message(conn, current, req.chat_message_id)
            await _ensure_workspace_chunk(conn, current.workspace_id, req.cited_chunk_id)
            existing_id = await conn.fetchval(
                "SELECT id FROM answer_feedback "
                "WHERE workspace_id=$1 AND chat_message_id=$2 AND reporter_user_id=$3",
                current.workspace_id,
                req.chat_message_id,
                current.user_id,
            )
            feedback_id = await conn.fetchval(
                "INSERT INTO answer_feedback("
                "workspace_id, chat_session_id, chat_message_id, reporter_user_id, "
                "cited_chunk_id, issue_type, status, note"
                ") VALUES($1, $2, $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (workspace_id, chat_message_id, reporter_user_id) "
                "DO UPDATE SET cited_chunk_id=EXCLUDED.cited_chunk_id, "
                "issue_type=EXCLUDED.issue_type, status=EXCLUDED.status, note=EXCLUDED.note, "
                "resolution_note=NULL, resolved_by=NULL, resolved_at=NULL, updated_at=now() "
                "RETURNING id",
                current.workspace_id,
                msg["session_id"],
                req.chat_message_id,
                current.user_id,
                req.cited_chunk_id,
                issue_type,
                status,
                note,
            )
            await auth.audit(
                conn,
                "answer_feedback_update" if existing_id else "answer_feedback_create",
                current.workspace_id,
                current.user_id,
                "answer_feedback",
                feedback_id,
                {
                    "chat_message_id": req.chat_message_id,
                    "issue_type": issue_type,
                    "status": status,
                    "cited_chunk_id": req.cited_chunk_id,
                },
            )
            return await _detail(conn, current.workspace_id, feedback_id)


@router.get("/mine")
async def my_feedback(
    chat_message_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Optional[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await _ensure_own_assistant_message(conn, current, chat_message_id)
        feedback_id = await conn.fetchval(
            "SELECT id FROM answer_feedback "
            "WHERE workspace_id=$1 AND chat_message_id=$2 AND reporter_user_id=$3",
            current.workspace_id,
            chat_message_id,
            current.user_id,
        )
        if feedback_id is None:
            return None
        return await _detail(conn, current.workspace_id, feedback_id)


@router.get("")
async def list_feedback(
    status: Optional[str] = None,
    issue_type: Optional[str] = None,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    args: List[Any] = [current.workspace_id]
    where = ["f.workspace_id=$1"]

    if status and status != "all":
        args.append(_clean_status(status))
        where.append(f"f.status=${len(args)}")

    if issue_type and issue_type != "all":
        args.append(_clean_issue_type(issue_type))
        where.append(f"f.issue_type=${len(args)}")
    elif not issue_type and (not status or status == "open"):
        where.append("f.issue_type <> 'helpful'")

    sql = (
        _FEEDBACK_SELECT
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY CASE f.status "
        "WHEN 'open' THEN 1 WHEN 'in_review' THEN 2 WHEN 'resolved' THEN 3 ELSE 4 END, "
        "f.updated_at DESC LIMIT 200"
    )
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [_row_to_feedback(r) for r in rows]


@router.post("/{feedback_id}/promote-regression")
async def promote_feedback_regression(
    feedback_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    """Freeze a confirmed answer failure into the workspace evaluation corpus."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            feedback = await conn.fetchrow(
                "SELECT f.id, f.workspace_id, f.status, f.issue_type, f.cited_chunk_id, "
                "m.content AS answer_snapshot, ("
                "  SELECT u.content FROM chat_messages u "
                "  WHERE u.session_id=f.chat_session_id AND u.role='user' "
                "  AND u.id < f.chat_message_id ORDER BY u.id DESC LIMIT 1"
                ") AS question "
                "FROM answer_feedback f JOIN chat_messages m ON m.id=f.chat_message_id "
                "WHERE f.id=$1 AND f.workspace_id=$2 FOR UPDATE",
                feedback_id, current.workspace_id,
            )
            if feedback is None:
                raise HTTPException(404, "feedback not found")
            if feedback["status"] != "resolved" or feedback["issue_type"] == "helpful":
                raise HTTPException(409, "only resolved non-helpful feedback can become a regression")
            if not feedback["question"]:
                raise HTTPException(422, "feedback answer has no preceding user question")
            case_id = await conn.fetchval(
                "INSERT INTO feedback_regression_cases("
                "workspace_id, feedback_id, question, issue_type, expected_citation_chunk_id, "
                "answer_snapshot, created_by_user_id"
                ") VALUES($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (feedback_id) DO NOTHING RETURNING id",
                current.workspace_id, feedback_id, feedback["question"], feedback["issue_type"],
                feedback["cited_chunk_id"], feedback["answer_snapshot"], current.user_id,
            )
            if case_id is None:
                case_id = await conn.fetchval(
                    "SELECT id FROM feedback_regression_cases WHERE feedback_id=$1",
                    feedback_id,
                )
            else:
                await auth.audit(
                    conn, "answer_feedback_promote_regression", current.workspace_id,
                    current.user_id, "feedback_regression_case", case_id,
                    {"feedback_id": feedback_id, "issue_type": feedback["issue_type"]},
                )
            row = await conn.fetchrow(
                "SELECT id, workspace_id, feedback_id, question, issue_type, "
                "expected_citation_chunk_id, created_by_user_id, created_at "
                "FROM feedback_regression_cases WHERE id=$1 AND workspace_id=$2",
                case_id, current.workspace_id,
            )
    return dict(row)


@router.get("/{feedback_id}")
async def get_feedback(
    feedback_id: int,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return await _detail(conn, current.workspace_id, feedback_id)


@router.patch("/{feedback_id}")
async def patch_feedback(
    feedback_id: int,
    req: FeedbackPatch,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, status FROM answer_feedback WHERE id=$1 AND workspace_id=$2",
                feedback_id,
                current.workspace_id,
            )
            if row is None:
                raise HTTPException(404, "feedback not found")

            assignments: List[str] = []
            args: List[Any] = [feedback_id, current.workspace_id]
            audit_data: Dict[str, Any] = {}
            next_status: Optional[str] = None

            if "status" in req.model_fields_set and req.status is not None:
                next_status = _clean_status(req.status)
                args.append(next_status)
                assignments.append(f"status=${len(args)}")
                audit_data["status"] = next_status
                if next_status in {"resolved", "dismissed"}:
                    args.append(current.user_id)
                    assignments.append(f"resolved_by=${len(args)}")
                    assignments.append("resolved_at=now()")
                else:
                    assignments.append("resolved_by=NULL")
                    assignments.append("resolved_at=NULL")

            if "resolution_note" in req.model_fields_set:
                note = _clean_note(req.resolution_note)
                args.append(note)
                assignments.append(f"resolution_note=${len(args)}")
                audit_data["resolution_note_changed"] = True

            if assignments:
                await conn.execute(
                    "UPDATE answer_feedback SET "
                    + ", ".join(assignments)
                    + ", updated_at=now() WHERE id=$1 AND workspace_id=$2",
                    *args,
                )
                if next_status == "resolved":
                    action = "answer_feedback_resolve"
                elif next_status == "dismissed":
                    action = "answer_feedback_dismiss"
                elif next_status:
                    action = "answer_feedback_status_change"
                else:
                    action = "answer_feedback_update"
                await auth.audit(
                    conn,
                    action,
                    current.workspace_id,
                    current.user_id,
                    "answer_feedback",
                    feedback_id,
                    audit_data,
                )
            return await _detail(conn, current.workspace_id, feedback_id)
