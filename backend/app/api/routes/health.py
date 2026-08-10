from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core import config, coordination, db, mailer
from app.domains.auth import service as auth
from app.domains.memory import worker
from app.domains.query import embedding_versions
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
        active_model_id = await embedding_versions.active_model(conn, current.workspace_id)
        active_model_key = (
            await embedding_versions.model_key(conn, active_model_id)
            if active_model_id is not None else None
        )
        active_coverage = (
            await embedding_versions.coverage(conn, current.workspace_id, active_model_id)
            if active_model_id is not None else None
        )
        corpus_models = await conn.fetch(
            "SELECT DISTINCT em.model_key AS embed_model "
            "FROM chunk_embeddings ce JOIN embedding_models em ON em.id=ce.embedding_model_id "
            "WHERE ce.workspace_id=$1 ORDER BY 1",
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
        "active_embedding_model_id": active_model_id,
        "active_embedding_model": active_model_key,
        "active_embedding_coverage": (
            {
                "active_chunks": active_coverage.active_chunks,
                "embedded_chunks": active_coverage.embedded_chunks,
                "complete": active_coverage.complete,
            } if active_coverage is not None else None
        ),
        "embedding_corpus_models": [r["embed_model"] for r in corpus_models],
        "embedding_space_consistent": all(
            r["embed_model"] == embed_model for r in corpus_models
        ) and active_model_key == embed_model and (
            active_coverage is None or active_coverage.complete
        ),
        "formation": await worker.queue_stats(current.workspace_id),
        "slack_events": bool(config.SLACK_SIGNING_SECRET),
        # False means password-reset and email-verification links are silently
        # discarded on this instance.
        "email_configured": mailer.configured(),
        "redis": await coordination.status(),
    }
