from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import config, coordination, db
from app.domains.auth import service as auth
from app.domains.memory import worker
from app.providers import llm
from app.providers.embeddings import active_embed_model, active_embedder

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


@router.get("/health/formation")
async def health_formation(request: Request) -> Dict[str, Any]:
    """Formation-queue health for external uptime monitors. Open by default
    (like /api/health); set HEALTH_TOKEN to require a matching token via the
    x-health-token header or ?token= query param."""
    if config.HEALTH_TOKEN:
        token = request.headers.get("x-health-token") or request.query_params.get("token")
        if token != config.HEALTH_TOKEN:
            raise HTTPException(401, "bad health token")
    return await worker.formation_health()


@router.get("/health/details")
async def health_details(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT 1")
        embed_model = await active_embed_model()
        corpus_models = await conn.fetch(
            "SELECT DISTINCT COALESCE(c.embed_model, 'legacy:unknown') AS embed_model "
            "FROM chunks c JOIN documents d ON d.id=c.document_id "
            "WHERE d.workspace_id=$1 ORDER BY 1",
            current.workspace_id,
        )
    return {
        "status": "ok" if ok == 1 else "degraded",
        "db": ok == 1,
        "llm_provider": llm.active_provider(),
        "llm_model": llm.active_model(),
        "llm_credentials": llm.credentials_available(),
        "embeddings": await active_embedder(),
        "embedding_model": embed_model,
        "embedding_corpus_models": [r["embed_model"] for r in corpus_models],
        "embedding_space_consistent": all(
            r["embed_model"] == embed_model for r in corpus_models
        ),
        "formation": await worker.queue_stats(current.workspace_id),
        "slack_events": bool(config.SLACK_SIGNING_SECRET),
        "redis": await coordination.status(),
    }
