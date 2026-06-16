"""Persisted chat conversations (the "Ask memory" UI)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import db
from app.domains.auth import service as auth

router = APIRouter(prefix="/api", tags=["sessions"])


class SessionCreate(BaseModel):
    title: str


class MessageCreate(BaseModel):
    role: str
    content: str
    meta: Optional[Dict[str, Any]] = None


@router.get("/sessions")
async def list_sessions(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, title, created_at, updated_at FROM chat_sessions "
            "WHERE workspace_id=$1 AND user_id=$2 ORDER BY updated_at DESC",
            current.workspace_id, current.user_id,
        )
    return [dict(r) for r in rows]


@router.post("/sessions")
async def create_session(
    req: SessionCreate,
    current: auth.AuthContext = Depends(auth.require_workspace),
) -> Dict[str, Any]:
    title = req.title.strip()[:120] or "New conversation"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO chat_sessions (workspace_id, user_id, title) VALUES ($1, $2, $3) "
            "RETURNING id, title, created_at, updated_at",
            current.workspace_id, current.user_id, title,
        )
    return dict(row)


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        sess = await conn.fetchrow(
            "SELECT id, title, created_at FROM chat_sessions "
            "WHERE id=$1 AND workspace_id=$2 AND user_id=$3",
            session_id, current.workspace_id, current.user_id,
        )
        if sess is None:
            raise HTTPException(404, "session not found")
        msgs = await conn.fetch(
            "SELECT id, role, content, meta, created_at FROM chat_messages "
            "WHERE session_id=$1 ORDER BY id",
            session_id,
        )
    out = dict(sess)
    out["messages"] = [dict(m) for m in msgs]
    return out


@router.post("/sessions/{session_id}/messages")
async def add_message(
    session_id: int,
    req: MessageCreate,
    current: auth.AuthContext = Depends(auth.require_workspace),
) -> Dict[str, Any]:
    if req.role not in ("user", "assistant"):
        raise HTTPException(400, "role must be user or assistant")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM chat_sessions WHERE id=$1 AND workspace_id=$2 AND user_id=$3",
            session_id, current.workspace_id, current.user_id,
        )
        if not exists:
            raise HTTPException(404, "session not found")
        row = await conn.fetchrow(
            "INSERT INTO chat_messages (session_id, role, content, meta) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            session_id, req.role, req.content, req.meta,
        )
        await conn.execute(
            "UPDATE chat_sessions SET updated_at=now() WHERE id=$1", session_id
        )
    return {"id": row["id"]}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM chat_sessions WHERE id=$1 AND workspace_id=$2 AND user_id=$3 RETURNING id",
            session_id, current.workspace_id, current.user_id,
        )
    if deleted is None:
        raise HTTPException(404, "session not found")
    return {"deleted": deleted}
