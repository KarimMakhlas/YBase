"""Per-workspace digest history (in-app delivery) and on-demand generation."""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from app.core import db
from app.domains.auth import service as auth
from app.domains.digest import service as digest

router = APIRouter(prefix="/api/digests", tags=["digests"])


@router.get("")
async def list_digests(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, period_start, period_end, payload, created_at FROM digests "
            "WHERE workspace_id=$1 ORDER BY created_at DESC LIMIT 20",
            current.workspace_id,
        )
    return [dict(r) for r in rows]


@router.get("/latest")
async def latest_digest(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, period_start, period_end, payload, created_at FROM digests "
            "WHERE workspace_id=$1 ORDER BY created_at DESC LIMIT 1",
            current.workspace_id,
        )
    return dict(row) if row else {}


@router.post("/run")
async def run_digest(
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    row = await digest.generate(current.workspace_id)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await auth.audit(conn, "digest_generate", current.workspace_id, current.user_id,
                         "digest", row["id"] if row else None)
    return row or {}
