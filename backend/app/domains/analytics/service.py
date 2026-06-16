"""Per-workspace usage analytics: activation + engagement over time.

Powers the admin Analytics dashboard and the 30-day-retention success metric.
Everything is workspace-scoped; metrics are derived from existing timestamps
(documents.ingested_at, chat_messages.created_at, memberships) plus the
activity_days daily-active signal.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from app.core import db
from app.domains.auth import service as auth
from app.domains.memory.scoring import node_score

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

LOW_CONFIDENCE = 0.4  # decisions scoring below this are flagged for review


@router.get("/overview")
async def analytics_overview(
    days: int = Query(30, ge=7, le=90),
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    ws = current.workspace_id
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            "SELECT "
            "  (SELECT count(*) FROM workspace_memberships WHERE workspace_id=$1) AS members, "
            "  (SELECT count(*) FROM workspace_memberships m JOIN users u ON u.id=m.user_id "
            "     WHERE m.workspace_id=$1 AND u.disabled) AS disabled_members, "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1) AS documents, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND archived_at IS NULL) AS memory_nodes, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL) AS decisions, "
            "  (SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "     WHERE s.workspace_id=$1 AND m.role='user') AS questions, "
            "  (SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "     WHERE s.workspace_id=$1 AND m.role='assistant') AS answers, "
            "  (SELECT count(DISTINCT user_id) FROM activity_days "
            "     WHERE workspace_id=$1 AND day >= (now() AT TIME ZONE 'utc')::date - 6) AS active_7d, "
            "  (SELECT count(DISTINCT user_id) FROM activity_days "
            "     WHERE workspace_id=$1 AND day >= (now() AT TIME ZONE 'utc')::date - 29) AS active_30d, "
            "  (SELECT count(*) FROM workspace_invites WHERE workspace_id=$1) AS invites",
            ws,
        )
        series = await conn.fetch(
            "WITH d AS ("
            "  SELECT generate_series("
            "    (now() AT TIME ZONE 'utc')::date - ($2::int - 1),"
            "    (now() AT TIME ZONE 'utc')::date, '1 day')::date AS day"
            ") "
            "SELECT to_char(d.day, 'YYYY-MM-DD') AS day, "
            "  (SELECT count(*) FROM documents doc WHERE doc.workspace_id=$1 "
            "     AND (doc.ingested_at AT TIME ZONE 'utc')::date = d.day) AS docs, "
            "  (SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "     WHERE s.workspace_id=$1 AND m.role='user' "
            "     AND (m.created_at AT TIME ZONE 'utc')::date = d.day) AS questions, "
            "  (SELECT count(DISTINCT a.user_id) FROM activity_days a "
            "     WHERE a.workspace_id=$1 AND a.day = d.day) AS active_users "
            "FROM d ORDER BY d.day",
            ws, days,
        )
        members = await conn.fetch(
            "SELECT u.id, u.display_name, u.email, m.role, u.disabled, "
            "  (SELECT max(a.day) FROM activity_days a "
            "     WHERE a.workspace_id=$1 AND a.user_id=u.id) AS last_active, "
            "  (SELECT count(*) FROM activity_days a "
            "     WHERE a.workspace_id=$1 AND a.user_id=u.id) AS active_days, "
            "  (SELECT count(*) FROM chat_messages cm JOIN chat_sessions cs ON cs.id=cm.session_id "
            "     WHERE cs.workspace_id=$1 AND cs.user_id=u.id AND cm.role='user') AS questions_asked "
            "FROM workspace_memberships m JOIN users u ON u.id=m.user_id "
            "WHERE m.workspace_id=$1 "
            "ORDER BY last_active DESC NULLS LAST, m.role, u.display_name",
            ws,
        )

    s = dict(summary)
    activation = [
        {"key": "team", "label": "Invited a teammate",
         "complete": s["members"] > 1 or s["invites"] > 0},
        {"key": "docs", "label": "Added documents", "complete": s["documents"] > 0},
        {"key": "memory", "label": "Formed memory", "complete": s["memory_nodes"] > 0},
        {"key": "asked", "label": "Asked a question", "complete": s["questions"] > 0},
        {"key": "returned", "label": "Came back another day",
         "complete": s["active_7d"] > 0 and any(
             r["active_users"] > 0 for r in series[:-1]
         )},
    ]
    return {
        "workspace": {"id": ws, "name": current.workspace_name},
        "range_days": days,
        "summary": s,
        "activation": {
            "steps": activation,
            "complete": all(a["complete"] for a in activation),
        },
        "timeseries": [dict(r) for r in series],
        "members": [dict(r) for r in members],
    }


def _check(key: str, label: str, status: str, detail: str) -> Dict[str, Any]:
    return {"key": key, "label": label, "status": status, "detail": detail}


@router.get("/quality")
async def memory_quality(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Memory-health scorecard: the same trust signals as scripts/eval.py
    (formation, density, topic/evidence coverage, graph density, confidence),
    surfaced in-app so extraction-quality regressions are visible."""
    ws = current.workspace_id
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        docs = await conn.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE formation_status='complete') AS complete, "
            "count(*) FILTER (WHERE formation_status='failed') AS failed, "
            "count(*) FILTER (WHERE formation_status IN ('pending','processing')) AS in_flight "
            "FROM documents WHERE workspace_id=$1", ws,
        )
        q = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE status='open') AS open, "
            "count(*) FILTER (WHERE status='resolved') AS resolved "
            "FROM memory_nodes WHERE workspace_id=$1 AND kind='question' AND archived_at IS NULL",
            ws,
        )
        edges = await conn.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE relation='about') AS about, "
            "count(*) FILTER (WHERE relation='revisits') AS revisits, "
            "count(*) FILTER (WHERE relation='resolves') AS resolves "
            "FROM memory_edges WHERE workspace_id=$1", ws,
        )
        # decisions with their topic-edge count, evidence count, status, and data
        decisions = await conn.fetch(
            "SELECT n.id, n.label, n.status, n.data, "
            "  (SELECT count(*) FROM memory_edges e JOIN memory_nodes t "
            "     ON t.id = CASE WHEN e.src=n.id THEN e.dst ELSE e.src END "
            "     WHERE (e.src=n.id OR e.dst=n.id) AND e.relation='about' AND t.kind='topic') AS topics, "
            "  (SELECT count(*) FROM chunk_links cl WHERE cl.node_id=n.id) AS evidence "
            "FROM memory_nodes n "
            "WHERE n.workspace_id=$1 AND n.kind='decision' AND n.archived_at IS NULL "
            "ORDER BY n.updated_at DESC", ws,
        )
        evidenceless = await conn.fetch(
            "SELECT n.id, n.label, n.kind FROM memory_nodes n "
            "WHERE n.workspace_id=$1 AND n.kind IN ('decision','question') AND n.archived_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM chunk_links cl WHERE cl.node_id=n.id) "
            "ORDER BY n.kind, n.label", ws,
        )

    total = docs["total"]
    dec_count = len(decisions)
    topicless = [{"id": d["id"], "label": d["label"]} for d in decisions if d["topics"] == 0]
    low_conf: List[Dict[str, Any]] = []
    for d in decisions:
        score = node_score(d["status"], d["data"], evidence_count=d["evidence"])
        if score < LOW_CONFIDENCE:
            low_conf.append({"id": d["id"], "label": d["label"], "confidence": score})
    per_doc = round(dec_count / total, 2) if total else 0.0
    epd = round(edges["total"] / dec_count, 2) if dec_count else 0.0

    checks = [
        _check(
            "formation",
            "Documents fully formed",
            "fail" if docs["failed"] else ("warn" if docs["in_flight"] else
                                           ("ok" if total and docs["complete"] == total else "warn")),
            f"{docs['complete']}/{total} complete"
            + (f", {docs['failed']} failed" if docs["failed"] else "")
            + (f", {docs['in_flight']} in flight" if docs["in_flight"] else ""),
        ),
        _check(
            "density", "Decisions per document",
            "ok" if 0.4 <= per_doc <= 4 else ("warn" if per_doc > 0 else "fail"),
            f"{per_doc} ({dec_count} decisions from {total} docs)",
        ),
        _check(
            "topics", "Decisions carry topics",
            "ok" if not topicless else "warn",
            f"{dec_count - len(topicless)}/{dec_count} have topics",
        ),
        _check(
            "evidence", "Memory cites its sources",
            "ok" if not evidenceless else "fail",
            f"{len(evidenceless)} node(s) cite no chunks"
            if evidenceless else "every decision and question is cited",
        ),
        _check(
            "graph", "Graph connectivity",
            "ok" if epd >= 2 else ("warn" if epd >= 1 else "fail"),
            f"{edges['total']} edges ({epd}/decision) — "
            f"revisits {edges['revisits']}, resolves {edges['resolves']}",
        ),
        _check(
            "confidence", "Decisions are confident",
            "ok" if not low_conf else "warn",
            f"{len(low_conf)} low-confidence decision(s)"
            if low_conf else "no low-confidence decisions",
        ),
    ]
    healthy = all(c["status"] == "ok" for c in checks)
    return {
        "workspace": {"id": ws, "name": current.workspace_name},
        "healthy": healthy,
        "checks": checks,
        "counts": {
            "documents": dict(docs),
            "decisions": dec_count,
            "questions_open": q["open"],
            "questions_resolved": q["resolved"],
            "edges": dict(edges),
        },
        "topicless": topicless,
        "evidenceless": [dict(r) for r in evidenceless],
        "low_confidence": low_conf,
    }
