from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import config, coordination, db, mailer
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
    """Formation-queue health for external uptime monitors.

    The full payload counts documents and failures across EVERY workspace on the
    instance, so it tells an anonymous caller how much data the whole tenant base
    is pushing through. Detail therefore requires HEALTH_TOKEN (via the
    x-health-token header or ?token=). Without a matching token the endpoint
    still answers liveness — enough for an uptime check to see the app is up and
    the queue isn't stalled — but withholds the numbers."""
    token = request.headers.get("x-health-token") or request.query_params.get("token")
    authorized = bool(config.HEALTH_TOKEN) and token == config.HEALTH_TOKEN
    if config.HEALTH_TOKEN and token is not None and not authorized:
        raise HTTPException(401, "bad health token")
    health = await worker.formation_health()
    if authorized:
        return health
    return {"status": "stalled" if health.get("stalled") else "ok", "detail": False}


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
        # False means password-reset and email-verification links are silently
        # discarded on this instance.
        "email_configured": mailer.configured(),
        "redis": await coordination.status(),
    }
