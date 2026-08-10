"""Admin MVP-readiness and recovery endpoints."""

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

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


@router.get("/pipeline-slo")
async def pipeline_slo(
    days: int = 7,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Durable revision-stage timing from acceptance through active formation."""
    days = max(1, min(days, 90))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            "SELECT count(*)::int AS accepted_revisions, "
            "count(*) FILTER (WHERE r.materialized_at IS NOT NULL)::int AS searchable_revisions, "
            "count(*) FILTER (WHERE fr.activated_at IS NOT NULL)::int AS formed_revisions, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY "
            "  extract(epoch FROM (r.materialized_at - r.created_at)) * 1000) "
            "  FILTER (WHERE r.materialized_at IS NOT NULL) AS p95_accepted_to_searchable_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY "
            "  extract(epoch FROM (fr.activated_at - r.materialized_at)) * 1000) "
            "  FILTER (WHERE fr.activated_at IS NOT NULL AND r.materialized_at IS NOT NULL) "
            "  AS p95_searchable_to_formed_ms "
            "FROM document_revisions r LEFT JOIN formation_runs fr "
            "ON fr.revision_id=r.id AND fr.is_active "
            "WHERE r.workspace_id=$1 AND r.created_at > now() - ($2 || ' days')::interval",
            current.workspace_id, str(days),
        )

    def _ms(value: Any) -> Any:
        return round(value) if value is not None else None

    return {
        "days": days,
        "accepted_revisions": summary["accepted_revisions"],
        "searchable_revisions": summary["searchable_revisions"],
        "formed_revisions": summary["formed_revisions"],
        "p95_accepted_to_searchable_ms": _ms(summary["p95_accepted_to_searchable_ms"]),
        "p95_searchable_to_formed_ms": _ms(summary["p95_searchable_to_formed_ms"]),
    }


@router.get("/query-slo")
async def query_slo(
    days: int = 7,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Query latency and grounding-verification percentiles for release gates."""
    days = max(1, min(days, 90))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            "SELECT count(*)::int AS runs, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms) AS p50_total_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS p95_total_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY retrieval_ms) AS p95_retrieval_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY generation_ms) AS p95_generation_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY verification_ms) AS p95_verification_ms, "
            "avg(citation_coverage) AS mean_citation_coverage, "
            "count(*) FILTER (WHERE claim_verification_status='passed')::int AS claims_passed, "
            "count(*) FILTER (WHERE claim_verification_status='failed')::int AS claims_failed, "
            "count(*) FILTER (WHERE claim_verification_status='not_checked')::int AS claims_not_checked, "
            "coalesce(sum(unsupported_claims), 0)::int AS unsupported_claims, "
            "coalesce(sum(contradicted_claims), 0)::int AS contradicted_claims "
            "FROM query_runs WHERE workspace_id=$1 AND status='success' "
            "AND created_at > now() - ($2 || ' days')::interval",
            current.workspace_id, str(days),
        )

    def _ms(value: Any) -> Any:
        return round(value) if value is not None else None

    return {
        "days": days,
        "runs": summary["runs"],
        "p50_total_ms": _ms(summary["p50_total_ms"]),
        "p95_total_ms": _ms(summary["p95_total_ms"]),
        "p95_retrieval_ms": _ms(summary["p95_retrieval_ms"]),
        "p95_generation_ms": _ms(summary["p95_generation_ms"]),
        "p95_verification_ms": _ms(summary["p95_verification_ms"]),
        "mean_citation_coverage": (
            round(float(summary["mean_citation_coverage"]), 3)
            if summary["mean_citation_coverage"] is not None else None
        ),
        "claim_verification": {
            "passed": summary["claims_passed"],
            "failed": summary["claims_failed"],
            "not_checked": summary["claims_not_checked"],
            "unsupported_claims": summary["unsupported_claims"],
            "contradicted_claims": summary["contradicted_claims"],
        },
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


# ── Fleet view (cross-workspace) ─────────────────────────────────────────────
# Unlike every other ops endpoint (admin on the ACTIVE workspace), the fleet
# endpoints span every workspace where the CURRENT USER is admin/owner — no
# new role needed, membership already encodes who may operate what.


def _operated_workspaces(current: auth.AuthContext) -> List[Dict[str, Any]]:
    return [w for w in current.workspaces if w.get("role") in ("admin", "owner")]


@router.get("/fleet")
async def fleet_overview(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """One card of operational vitals per operated workspace: formation queue
    depth, failures, pending agent proposals, connector health, 24h usage,
    and a 24h formation-latency SLO snapshot. Set-based queries grouped by
    workspace — cost does not grow per workspace."""
    operated = _operated_workspaces(current)
    if not operated:
        return {"workspaces": []}
    ids = [w["id"] for w in operated]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        docs = await conn.fetch(
            "SELECT workspace_id, "
            "       count(*) FILTER (WHERE formation_status IN ('pending','processing')) AS queue_depth, "
            "       count(*) FILTER (WHERE formation_status='failed') AS failed_docs, "
            "       count(*) FILTER (WHERE formation_status='rate_limited') AS rate_limited_docs, "
            "       count(*) AS documents "
            "FROM documents WHERE workspace_id = ANY($1::int[]) GROUP BY workspace_id",
            ids,
        )
        memory = await conn.fetch(
            "SELECT workspace_id, "
            "       count(*) FILTER (WHERE kind='decision') AS decisions, "
            "       count(*) FILTER (WHERE curated_at IS NULL) AS needs_review "
            "FROM memory_nodes WHERE workspace_id = ANY($1::int[]) AND archived_at IS NULL "
            "GROUP BY workspace_id",
            ids,
        )
        proposals = await conn.fetch(
            "SELECT workspace_id, count(*) AS pending_proposals "
            "FROM memory_proposals WHERE workspace_id = ANY($1::int[]) AND status='pending' "
            "GROUP BY workspace_id",
            ids,
        )
        connectors = await conn.fetch(
            "SELECT c.workspace_id, c.provider, c.status, c.last_sync_at, c.last_error, "
            "       (SELECT count(*) FROM sync_jobs j WHERE j.connection_id=c.id "
            "        AND j.status='failed') AS failed_jobs "
            "FROM source_connections c WHERE c.workspace_id = ANY($1::int[]) "
            "ORDER BY c.workspace_id, c.provider",
            ids,
        )
        usage_24h = await conn.fetch(
            "SELECT workspace_id, "
            "       coalesce(sum(total_tokens), 0)::bigint AS tokens_24h, "
            "       coalesce(sum(request_count), 0)::int AS requests_24h "
            "FROM usage_events WHERE workspace_id = ANY($1::int[]) "
            "AND created_at > now() - interval '24 hours' GROUP BY workspace_id",
            ids,
        )
        slo = await conn.fetch(
            "SELECT workspace_id, count(*) AS runs, "
            "       count(*) FILTER (WHERE status IN ('failed','timeout')) AS failures, "
            "       percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms) AS p50_ms, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms "
            "FROM formation_runs WHERE workspace_id = ANY($1::int[]) "
            "AND started_at > now() - interval '24 hours' GROUP BY workspace_id",
            ids,
        )

    by_ws = lambda rows: {r["workspace_id"]: r for r in rows}  # noqa: E731
    docs_m, mem_m, prop_m, use_m, slo_m = (
        by_ws(docs), by_ws(memory), by_ws(proposals), by_ws(usage_24h), by_ws(slo))
    conn_m: Dict[int, List[Dict[str, Any]]] = {}
    for c in connectors:
        conn_m.setdefault(c["workspace_id"], []).append({
            "provider": c["provider"],
            "status": c["status"],
            "last_sync_at": c["last_sync_at"].isoformat() if c["last_sync_at"] else None,
            "last_error": c["last_error"],
            "failed_jobs": c["failed_jobs"],
        })

    out = []
    for w in operated:
        wid = w["id"]
        d, m, p, u, s = (docs_m.get(wid), mem_m.get(wid), prop_m.get(wid),
                         use_m.get(wid), slo_m.get(wid))
        conns = conn_m.get(wid, [])
        out.append({
            "workspace_id": wid,
            "name": w["name"],
            "role": w["role"],
            "is_active": wid == current.workspace_id,
            "queue_depth": d["queue_depth"] if d else 0,
            "failed_docs": d["failed_docs"] if d else 0,
            "rate_limited_docs": d["rate_limited_docs"] if d else 0,
            "documents": d["documents"] if d else 0,
            "decisions": m["decisions"] if m else 0,
            "needs_review": m["needs_review"] if m else 0,
            "pending_proposals": p["pending_proposals"] if p else 0,
            "connectors": conns,
            "failing_connectors": [c["provider"] for c in conns
                                   if c["status"] == "error" or c["failed_jobs"]],
            "tokens_24h": u["tokens_24h"] if u else 0,
            "requests_24h": u["requests_24h"] if u else 0,
            "slo_24h": {
                "runs": s["runs"] if s else 0,
                "failures": s["failures"] if s else 0,
                "p50_ms": round(s["p50_ms"]) if s and s["p50_ms"] is not None else None,
                "p95_ms": round(s["p95_ms"]) if s and s["p95_ms"] is not None else None,
            },
        })
    return {"workspaces": out}


@router.get("/fleet/activity")
async def fleet_activity(
    workspace_id: Optional[int] = None,
    limit: int = 50,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Merged audit feed across operated workspaces (or one of them), newest
    first, with actor display names resolved."""
    operated = _operated_workspaces(current)
    ids = [w["id"] for w in operated]
    if workspace_id is not None:
        if workspace_id not in ids:
            raise HTTPException(403, "not an admin of that workspace")
        ids = [workspace_id]
    if not ids:
        return {"events": []}
    limit = max(1, min(limit, 200))
    names = {w["id"]: w["name"] for w in operated}
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT a.id, a.workspace_id, a.actor_user_id, u.display_name AS actor_name, "
            "       a.action, a.target_type, a.target_id, a.data, a.created_at "
            "FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id "
            "WHERE a.workspace_id = ANY($1::int[]) "
            "ORDER BY a.created_at DESC, a.id DESC LIMIT $2",
            ids, limit,
        )
    return {"events": [
        {
            "id": r["id"],
            "workspace_id": r["workspace_id"],
            "workspace_name": names.get(r["workspace_id"]),
            "actor": r["actor_name"],
            "action": r["action"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "data": r["data"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]}


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
