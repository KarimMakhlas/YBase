"""Periodic per-workspace digest: what's new since last time.

Provider-agnostic delivery: every digest is stored for in-app display, and
additionally emailed when an email provider (Resend) is configured. Without a
key the email channel is a logged no-op, so this works locally today and a real
inbox is a config change away.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core import config, db, mailer as email
from app.core.dates import iso_date

log = logging.getLogger("ybase.digest")


async def compute_digest(conn, workspace_id: int, since: datetime, until: datetime) -> Dict[str, Any]:
    new_decisions = await conn.fetch(
        "SELECT id, label, status, data, created_at FROM memory_nodes "
        "WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL "
        "AND created_at >= $2 AND created_at < $3 ORDER BY created_at DESC",
        workspace_id, since, until,
    )
    resolved = await conn.fetch(
        "SELECT id, label FROM memory_nodes "
        "WHERE workspace_id=$1 AND kind='question' AND status='resolved' AND archived_at IS NULL "
        "AND updated_at >= $2 AND updated_at < $3 ORDER BY updated_at DESC",
        workspace_id, since, until,
    )
    opened = await conn.fetch(
        "SELECT id, label FROM memory_nodes "
        "WHERE workspace_id=$1 AND kind='question' AND status='open' AND archived_at IS NULL "
        "AND created_at >= $2 AND created_at < $3 ORDER BY created_at DESC",
        workspace_id, since, until,
    )
    new_documents = await conn.fetchval(
        "SELECT count(*) FROM documents WHERE workspace_id=$1 "
        "AND ingested_at >= $2 AND ingested_at < $3",
        workspace_id, since, until,
    )
    stale = await conn.fetch(
        "SELECT id, label, (EXTRACT(DAY FROM now() - created_at))::int AS age_days "
        "FROM memory_nodes WHERE workspace_id=$1 AND kind='question' AND status='open' "
        "AND archived_at IS NULL AND created_at < now() - ($2 || ' days')::interval "
        "ORDER BY created_at LIMIT 5",
        workspace_id, str(config.STALE_QUESTION_DAYS),
    )

    def decision(r) -> Dict[str, Any]:
        data = r["data"] or {}
        return {"id": r["id"], "title": r["label"], "status": r["status"],
                "date": data.get("date") or iso_date(r["created_at"])}

    new_decisions_l = [decision(r) for r in new_decisions]
    resolved_l = [{"id": r["id"], "title": r["label"]} for r in resolved]
    opened_l = [{"id": r["id"], "title": r["label"]} for r in opened]
    empty = not (new_decisions_l or resolved_l or opened_l or new_documents)
    return {
        "new_documents": new_documents,
        "new_decisions": new_decisions_l,
        "resolved_questions": resolved_l,
        "opened_questions": opened_l,
        "stale_questions": [
            {"id": r["id"], "title": r["label"], "age_days": r["age_days"]} for r in stale
        ],
        "empty": empty,
    }


def render_text(workspace_name: str, payload: Dict[str, Any]) -> str:
    """Plain-text/markdown digest body — used for email and previews."""
    lines = [f"# {workspace_name} — memory digest", ""]
    if payload["empty"]:
        lines.append("A quiet week — no new decisions or questions.")
    if payload["new_documents"]:
        lines.append(f"**{payload['new_documents']}** new document(s) remembered.")
    if payload["new_decisions"]:
        lines.append("\n## New decisions")
        for d in payload["new_decisions"]:
            lines.append(f"- {d['title']} ({d['status']}{', ' + d['date'] if d['date'] else ''})")
    if payload["resolved_questions"]:
        lines.append("\n## Questions resolved")
        for q in payload["resolved_questions"]:
            lines.append(f"- {q['title']}")
    if payload["opened_questions"]:
        lines.append("\n## New open questions")
        for q in payload["opened_questions"]:
            lines.append(f"- {q['title']}")
    if payload["stale_questions"]:
        lines.append("\n## Still unanswered")
        for q in payload["stale_questions"]:
            lines.append(f"- {q['title']} ({q['age_days']}d)")
    return "\n".join(lines)


async def _send_email(conn, workspace_id: int, workspace_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Email the digest to active workspace members (no-op without a provider)."""
    if not email.configured():
        return {"status": "skipped", "reason": "no email provider configured"}
    rows = await conn.fetch(
        "SELECT u.email FROM workspace_memberships m JOIN users u ON u.id=m.user_id "
        "WHERE m.workspace_id=$1 AND NOT u.disabled", workspace_id,
    )
    return await email.send(
        [r["email"] for r in rows],
        f"{workspace_name} — your memory digest",
        render_text(workspace_name, payload),
    )


async def generate(workspace_id: int, since: Optional[datetime] = None,
                   until: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Compute, store, and deliver one digest for a workspace. Returns the
    stored row (dict) or None if the workspace is unknown."""
    now = datetime.now(timezone.utc)
    until = until or now
    if since is None:
        since = until - timedelta(seconds=config.DIGEST_INTERVAL_S)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        ws = await conn.fetchrow("SELECT id, name FROM workspaces WHERE id=$1", workspace_id)
        if ws is None:
            return None
        payload = await compute_digest(conn, workspace_id, since, until)
        email_result = await _send_email(conn, workspace_id, ws["name"], payload)
        channels = {"in_app": {"status": "stored"}, "email": email_result}
        row = await conn.fetchrow(
            "INSERT INTO digests(workspace_id, period_start, period_end, payload, channels) "
            "VALUES($1, $2, $3, $4, $5) "
            "RETURNING id, period_start, period_end, payload, channels, created_at",
            workspace_id, since, until, payload, channels,
        )
    return dict(row)


_last_tick: Optional[datetime] = None


async def run_digest_tick() -> int:
    """Generate digests for workspaces whose last digest is older than the
    interval. Self-throttled so the worker idle loop can call it freely."""
    global _last_tick
    if not (config.DIGEST_ENABLED and config.DIGEST_INTERVAL_S):
        return 0
    now = datetime.now(timezone.utc)
    # don't re-scan more than once every 5 minutes
    if _last_tick is not None and (now - _last_tick).total_seconds() < 300:
        return 0
    _last_tick = now
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        due = await conn.fetch(
            "SELECT w.id, "
            "  (SELECT max(period_end) FROM digests g WHERE g.workspace_id=w.id) AS last_end "
            "FROM workspaces w "
            "WHERE EXISTS (SELECT 1 FROM documents d WHERE d.workspace_id=w.id) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM digests g WHERE g.workspace_id=w.id "
            "  AND g.created_at > now() - ($1 || ' seconds')::interval)",
            str(config.DIGEST_INTERVAL_S),
        )
    count = 0
    for r in due:
        since = r["last_end"] or (now - timedelta(seconds=config.DIGEST_INTERVAL_S))
        await generate(r["id"], since=since, until=now)
        count += 1
    if count:
        log.info("generated %d workspace digest(s)", count)
    return count
