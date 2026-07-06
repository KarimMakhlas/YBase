"""Admin MVP-readiness and recovery endpoints."""

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

from app.core import config, db
from app.domains.auth import service as auth
from app.domains.documents.ingestion import schedule_formation
from app.domains.memory import worker
from app.domains.ops.demo_data import DEMO_QUESTIONS, seed_demo_data as _seed_demo_data
from app.providers import llm

router = APIRouter(prefix="/api/ops", tags=["ops"])
log = logging.getLogger("ybase.ops")


def _cost_rates() -> Dict[str, Dict[str, float]]:
    """Optional {model: {input_per_mtok, output_per_mtok}} from COST_RATES_JSON.
    Empty/invalid JSON → tokens-only reporting."""
    if not config.COST_RATES_JSON:
        return {}
    try:
        rates = json.loads(config.COST_RATES_JSON)
        return rates if isinstance(rates, dict) else {}
    except json.JSONDecodeError:
        log.warning("COST_RATES_JSON is not valid JSON — cost annotation disabled")
        return {}


def _cost_usd(model: str, input_tokens: int, output_tokens: int,
              rates: Dict[str, Dict[str, float]]) -> Optional[float]:
    r = rates.get(model)
    if not isinstance(r, dict):
        return None
    return round(
        (input_tokens / 1e6) * float(r.get("input_per_mtok", 0))
        + (output_tokens / 1e6) * float(r.get("output_per_mtok", 0)),
        4,
    )


def _step(
    key: str,
    label: str,
    complete: bool,
    detail: str,
    action: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "complete": complete,
        "detail": detail,
        "action": action,
    }


@router.get("/overview")
async def ops_overview(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1) AS documents, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1 AND formation_status='complete') AS documents_complete, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1 AND formation_status='pending') AS documents_pending, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1 AND formation_status='processing') AS documents_processing, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1 AND formation_status='failed') AS documents_failed, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1 AND formation_status='rate_limited') AS documents_rate_limited, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND archived_at IS NULL) AS memory_nodes, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL) AS decisions, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='question' AND archived_at IS NULL) AS questions, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND curated_at IS NULL AND archived_at IS NULL) AS needs_review, "
            "  (SELECT count(*) FROM answer_feedback WHERE workspace_id=$1 AND status IN ('open','in_review') "
            "     AND issue_type <> 'helpful') AS open_feedback, "
            "  (SELECT count(*) FROM source_connections WHERE workspace_id=$1) AS source_connections, "
            "  (SELECT count(*) FROM source_streams WHERE workspace_id=$1 AND selected) AS selected_streams, "
            "  (SELECT count(*) FROM sync_jobs WHERE workspace_id=$1 AND status IN ('pending','running','paused')) AS active_sync_jobs, "
            "  (SELECT count(*) FROM sync_jobs WHERE workspace_id=$1 AND status='failed') AS failed_sync_jobs, "
            "  (SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "     WHERE s.workspace_id=$1 AND m.role='assistant') AS assistant_messages",
            current.workspace_id,
        )
        failed_docs = await conn.fetch(
            "SELECT id, source, title, formation_error, formation_attempts, ingested_at "
            "FROM documents WHERE workspace_id=$1 AND formation_status='failed' "
            "ORDER BY ingested_at DESC LIMIT 12",
            current.workspace_id,
        )
        active_docs = await conn.fetch(
            "SELECT id, source, title, formation_status, ingested_at "
            "FROM documents WHERE workspace_id=$1 AND formation_status IN ('pending','processing') "
            "ORDER BY ingested_at LIMIT 12",
            current.workspace_id,
        )
        rate_limited_docs = await conn.fetch(
            "SELECT id, source, title, formation_next_attempt_at, ingested_at "
            "FROM documents WHERE workspace_id=$1 AND formation_status='rate_limited' "
            "ORDER BY ingested_at LIMIT 12",
            current.workspace_id,
        )
        sources = await conn.fetch(
            "SELECT c.id, c.provider, c.name, c.status, c.last_sync_at, c.last_error, "
            "       (SELECT count(*) FROM source_streams s WHERE s.connection_id=c.id) AS stream_count, "
            "       (SELECT count(*) FROM source_streams s WHERE s.connection_id=c.id AND s.selected) AS selected_count "
            "FROM source_connections c WHERE c.workspace_id=$1 ORDER BY c.created_at DESC",
            current.workspace_id,
        )
        sync_jobs = await conn.fetch(
            "SELECT j.id, j.connection_id, c.name AS connection_name, j.provider, j.kind, "
            "       j.status, j.stats, j.error, j.next_retry_at, j.created_at, j.updated_at "
            "FROM sync_jobs j JOIN source_connections c ON c.id=j.connection_id "
            "WHERE j.workspace_id=$1 AND j.status <> 'complete' "
            "ORDER BY j.updated_at DESC LIMIT 12",
            current.workspace_id,
        )
    c = dict(counts)
    q = await worker.queue_stats(current.workspace_id)
    has_docs_or_source = c["documents"] > 0 or c["selected_streams"] > 0
    memory_ready = c["documents_complete"] > 0 and c["memory_nodes"] > 0
    no_pipeline_failures = c["documents_failed"] == 0 and c["failed_sync_jobs"] == 0
    steps = [
        _step(
            "add_memory",
            "Add or connect a source",
            has_docs_or_source,
            f"{c['documents']} docs ingested, {c['selected_streams']} Slack channels selected.",
            "sources" if c["source_connections"] == 0 else "add",
        ),
        _step(
            "formation",
            "Let memory formation finish",
            c["documents"] > 0 and c["documents_pending"] == 0 and c["documents_processing"] == 0,
            f"{c['documents_complete']} complete, {c['documents_pending']} pending, {c['documents_processing']} processing.",
            "ops",
        ),
        _step(
            "memory_ready",
            "Confirm useful memory exists",
            memory_ready,
            f"{c['decisions']} decisions, {c['questions']} questions, {c['memory_nodes']} total memory nodes.",
            "review" if c["memory_nodes"] else "add",
        ),
        _step(
            "ask_first",
            "Ask the first cited question",
            c["assistant_messages"] > 0,
            f"{c['assistant_messages']} saved assistant answers.",
            "chat",
        ),
        _step(
            "trust_loop",
            "Triage trust signals",
            c["needs_review"] == 0 and c["open_feedback"] == 0,
            f"{c['needs_review']} memory nodes need review, {c['open_feedback']} feedback items open.",
            "feedback" if c["open_feedback"] else "review",
        ),
        _step(
            "recovery",
            "Clear pipeline failures",
            no_pipeline_failures,
            f"{c['documents_failed']} failed docs, {c['failed_sync_jobs']} failed sync jobs.",
            "ops",
        ),
    ]
    return {
        "workspace": {
            "id": current.workspace_id,
            "name": current.workspace_name,
            "role": current.role,
        },
        "counts": c,
        "readiness": {
            "complete": all(s["complete"] for s in steps),
            "steps": steps,
        },
        "formation": q,
        "provider": {
            "llm_provider": llm.active_provider(),
            "llm_model": llm.active_model(),
            "slack_configured": bool(
                config.SLACK_CLIENT_ID
                and config.SLACK_CLIENT_SECRET
                and config.SLACK_SIGNING_SECRET
                and config.CONNECTOR_SECRET_KEY
            ),
        },
        "failed_documents": [dict(r) for r in failed_docs],
        "active_documents": [dict(r) for r in active_docs],
        "rate_limited_documents": [dict(r) for r in rate_limited_docs],
        "sources": [dict(r) for r in sources],
        "sync_jobs": [dict(r) for r in sync_jobs],
        "demo_questions": DEMO_QUESTIONS,
    }


@router.get("/slo")
async def formation_slo(
    days: int = 7,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Formation latency/throughput percentiles for this workspace, from the
    per-run SLO table. NULL-duration rows are excluded from percentiles by
    the ordered-set aggregates themselves."""
    days = max(1, min(days, 90))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            "SELECT count(*) AS runs, "
            "       count(*) FILTER (WHERE status='success') AS successes, "
            "       count(*) FILTER (WHERE status='failed') AS failures, "
            "       count(*) FILTER (WHERE status='timeout') AS timeouts, "
            "       percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms) AS p50_ms, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY queue_wait_ms) AS p95_queue_wait_ms, "
            "       percentile_cont(0.95) WITHIN GROUP "
            "         (ORDER BY (stage_timings->>'llm')::float) AS p95_llm_ms "
            "FROM formation_runs "
            "WHERE workspace_id=$1 AND started_at > now() - ($2 || ' days')::interval",
            current.workspace_id, str(days),
        )
        series = await conn.fetch(
            "SELECT date_trunc('day', started_at)::date AS day, "
            "       count(*) AS runs, "
            "       count(*) FILTER (WHERE status='success') AS successes, "
            "       count(*) FILTER (WHERE status IN ('failed','timeout')) AS failures, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms "
            "FROM formation_runs "
            "WHERE workspace_id=$1 AND started_at > now() - ($2 || ' days')::interval "
            "GROUP BY 1 ORDER BY 1",
            current.workspace_id, str(days),
        )
    queue = await worker.queue_stats(current.workspace_id)

    def _ms(v: Any) -> Any:
        return round(v) if v is not None else None

    return {
        "days": days,
        "runs": summary["runs"],
        "successes": summary["successes"],
        "failures": summary["failures"],
        "timeouts": summary["timeouts"],
        "p50_ms": _ms(summary["p50_ms"]),
        "p95_ms": _ms(summary["p95_ms"]),
        "p95_queue_wait_ms": _ms(summary["p95_queue_wait_ms"]),
        "p95_llm_ms": _ms(summary["p95_llm_ms"]),
        "per_day": [
            {
                "day": r["day"].isoformat(),
                "runs": r["runs"],
                "successes": r["successes"],
                "failures": r["failures"],
                "p95_ms": _ms(r["p95_ms"]),
            }
            for r in series
        ],
        "queue": queue,
    }


@router.get("/usage")
async def usage_report(
    days: int = 30,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Token/request usage for this workspace: totals, per-day series, and a
    (surface, provider, model) breakdown. Dollar figures appear only when
    COST_RATES_JSON prices the model."""
    days = max(1, min(days, 365))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        breakdown = await conn.fetch(
            "SELECT surface, kind, provider, model, "
            "       sum(request_count)::int AS requests, "
            "       coalesce(sum(input_tokens), 0)::bigint AS input_tokens, "
            "       coalesce(sum(output_tokens), 0)::bigint AS output_tokens, "
            "       coalesce(sum(total_tokens), 0)::bigint AS total_tokens "
            "FROM usage_events "
            "WHERE workspace_id=$1 AND created_at > now() - ($2 || ' days')::interval "
            "GROUP BY surface, kind, provider, model "
            "ORDER BY total_tokens DESC, requests DESC",
            current.workspace_id, str(days),
        )
        series = await conn.fetch(
            "SELECT date_trunc('day', created_at)::date AS day, "
            "       sum(request_count)::int AS requests, "
            "       coalesce(sum(total_tokens), 0)::bigint AS total_tokens "
            "FROM usage_events "
            "WHERE workspace_id=$1 AND created_at > now() - ($2 || ' days')::interval "
            "GROUP BY 1 ORDER BY 1",
            current.workspace_id, str(days),
        )
    rates = _cost_rates()
    rows = []
    total_cost: Optional[float] = 0.0 if rates else None
    for r in breakdown:
        row = dict(r)
        cost = _cost_usd(r["model"], r["input_tokens"], r["output_tokens"], rates)
        row["cost_usd"] = cost
        if cost is not None and total_cost is not None:
            total_cost = round(total_cost + cost, 4)
        rows.append(row)
    return {
        "days": days,
        "requests": sum(r["requests"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "total_tokens": sum(r["total_tokens"] for r in rows),
        "cost_usd": total_cost,
        "breakdown": rows,
        "per_day": [
            {"day": r["day"].isoformat(), "requests": r["requests"],
             "total_tokens": r["total_tokens"]}
            for r in series
        ],
    }


@router.get("/audit")
async def audit_log(
    days: int = 30,
    actions: Optional[str] = None,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Workspace audit trail (newest first, capped at 200). `actions` is an
    optional comma-separated filter, e.g.
    actions=consolidation_merge_nodes,formation_failed_permanently."""
    days = max(1, min(days, 365))
    wanted = [a.strip() for a in (actions or "").split(",") if a.strip()]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if wanted:
            rows = await conn.fetch(
                "SELECT id, actor_user_id, action, target_type, target_id, data, created_at "
                "FROM audit_events WHERE workspace_id=$1 "
                "AND created_at > now() - ($2 || ' days')::interval "
                "AND action = ANY($3::text[]) "
                "ORDER BY created_at DESC, id DESC LIMIT 200",
                current.workspace_id, str(days), wanted,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, actor_user_id, action, target_type, target_id, data, created_at "
                "FROM audit_events WHERE workspace_id=$1 "
                "AND created_at > now() - ($2 || ' days')::interval "
                "ORDER BY created_at DESC, id DESC LIMIT 200",
                current.workspace_id, str(days),
            )
    return {"days": days, "events": [dict(r) for r in rows]}


@router.post("/failed-documents/retry")
async def retry_failed_documents(
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "UPDATE documents SET formation_status='pending', formation_error=NULL, "
                "formation_attempts=0, formation_next_attempt_at=now() "
                "WHERE workspace_id=$1 AND formation_status='failed' RETURNING id",
                current.workspace_id,
            )
            await auth.audit(
                conn,
                "retry_failed_documents",
                current.workspace_id,
                current.user_id,
                data={"count": len(rows), "document_ids": [r["id"] for r in rows]},
            )
    for r in rows:
        await schedule_formation(r["id"])
    return {"requeued": len(rows), "document_ids": [r["id"] for r in rows]}


@router.post("/demo-seed")
async def seed_demo_data(
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    result = await _seed_demo_data(current.workspace_id)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await auth.audit(
            conn,
            "demo_seed",
            current.workspace_id,
            current.user_id,
            data={
                "created": result["created"],
                "duplicates": result["duplicates"],
                "document_ids": result["document_ids"],
            },
        )
    return result
