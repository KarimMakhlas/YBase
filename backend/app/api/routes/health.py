from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.core import config, db
from app.domains.auth import service as auth
from app.domains.memory import worker
from app.providers import llm
from app.providers.embeddings import active_embedder

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1")
    return {
        "status": "ok" if ok == 1 else "degraded",
        "db": ok == 1,
        "needs_bootstrap": await auth.bootstrap_needed(),
    }


@router.get("/health/details")
async def health_details(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1")
    return {
        "status": "ok" if ok == 1 else "degraded",
        "db": ok == 1,
        "llm_provider": llm.active_provider(),
        "llm_model": llm.active_model(),
        "llm_credentials": llm.credentials_available(),
        "embeddings": await active_embedder(),
        "formation": await worker.queue_stats(current.workspace_id),
        "slack_events": bool(config.SLACK_SIGNING_SECRET),
    }
