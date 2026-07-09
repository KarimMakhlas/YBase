"""Source connector APIs and Slack sync jobs."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core import config, db
from app.core.crypto import decrypt_secret as _decrypt_secret
from app.domains.connectors import slack_reconcile_days
from app.domains.auth import service as auth
from app.domains.connectors.confluence import client as confluence
from app.domains.connectors.confluence import routes as confluence_routes
from app.domains.connectors.discord import client as discord
from app.domains.connectors.discord import routes as discord_routes
from app.domains.connectors.figma import client as figma
from app.domains.connectors.figma import routes as figma_routes
from app.domains.connectors.github import client as github
from app.domains.connectors.github import routes as github_routes
from app.domains.connectors.googledocs import client as googledocs
from app.domains.connectors.googledocs import routes as googledocs_routes
from app.domains.connectors.jira import client as jira
from app.domains.connectors.jira import routes as jira_routes
from app.domains.connectors.linear import client as linear
from app.domains.connectors.linear import routes as linear_routes
from app.domains.connectors.notion import client as notion
from app.domains.connectors.notion import routes as notion_routes
from app.domains.connectors.slack import events as slack
from app.domains.connectors.slack import routes as slack_routes
from app.domains.documents.ingestion import IngestRequest, ingest_document

router = APIRouter(prefix="/api", tags=["sources"])
router.include_router(slack_routes.router)
router.include_router(jira_routes.router)
router.include_router(github_routes.router)
router.include_router(linear_routes.router)
router.include_router(confluence_routes.router)
router.include_router(discord_routes.router)
router.include_router(googledocs_routes.router)
router.include_router(notion_routes.router)
router.include_router(figma_routes.router)

log = logging.getLogger("ybase.connectors")

SLACK_API = "https://slack.com/api"
_sync_tasks: Dict[asyncio.Task, Tuple[str, int]] = {}


class StreamPatch(BaseModel):
    selected: bool


class SyncRequest(BaseModel):
    days: int = 90
    # When set, after this (fast-slice) backfill completes, chain a second
    # backfill of `then_full_days` in the background. Used by onboarding to show
    # recent memory fast, then deepen history. Ignored on ordinary manual syncs.
    then_full_days: Optional[int] = None


class SlackRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class SlackAPIError(Exception):
    pass


def _frontend_from_request(request: Request) -> str:
    """Return a safe frontend redirect, never an arbitrary Referer origin."""
    configured = [config.APP_BASE_URL, *config.CORS_ORIGINS]
    allowed = set()
    for raw in configured:
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            allowed.add((parsed.scheme.lower(), parsed.netloc.lower()))

    fallback = config.APP_BASE_URL.rstrip("/") or "http://localhost:5173"
    ref = request.headers.get("referer")
    if not ref:
        return fallback
    parsed = urlsplit(ref)
    origin = (parsed.scheme.lower(), parsed.netloc.lower())
    if origin not in allowed:
        return fallback
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", "")).rstrip("/")


async def _slack_api(
    token: str, method: str, params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.get(
            f"{SLACK_API}/{method}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
        )
    if res.status_code == 429:
        retry_after = int(res.headers.get("Retry-After", "60"))
        raise SlackRateLimit(retry_after)
    res.raise_for_status()
    data = res.json()
    if not data.get("ok"):
        raise SlackAPIError(data.get("error", "slack_api_error"))
    return data


async def _slack_post(method: str, data: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(f"{SLACK_API}/{method}", data=data)
    res.raise_for_status()
    payload = res.json()
    if not payload.get("ok"):
        raise SlackAPIError(payload.get("error", "slack_api_error"))
    return payload


async def _connection(conn, workspace_id: int, connection_id: int):
    row = await conn.fetchrow(
        "SELECT * FROM source_connections WHERE id=$1 AND workspace_id=$2",
        connection_id, workspace_id,
    )
    if row is None:
        raise HTTPException(404, "source connection not found")
    return row


def _dispatch_sync(provider: str, job_id: int) -> None:
    """Run a sync job on the right connector's background coroutine, then chain a
    follow-up full backfill if this job requested one (the onboarding fast-slice)."""
    runner = {
        "jira": jira.run_sync_job, "github": github.run_sync_job,
        "linear": linear.run_sync_job, "confluence": confluence.run_sync_job,
        "discord": discord.run_sync_job, "googledocs": googledocs.run_sync_job,
        "notion": notion.run_sync_job, "figma": figma.run_sync_job,
    }.get(provider, run_slack_sync_job)
    task = asyncio.create_task(_run_then_chain(runner, provider, job_id))
    _sync_tasks[task] = (provider, job_id)

    def _finished(done: asyncio.Task) -> None:
        _sync_tasks.pop(done, None)
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error:
            log.error("sync task %s/%d crashed: %s", provider, job_id, error)

    task.add_done_callback(_finished)


async def stop_sync_tasks() -> None:
    """Cancel connector tasks during graceful shutdown.

    A canceled task is intentionally left recoverable in Postgres; the startup
    janitor below requeues it after the stale threshold if the process dies
    before a connector can write its final state.
    """
    tracked = list(_sync_tasks.items())
    tasks = [task for task, _ in tracked]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if tracked:
        pool = await db.get_pool()
        reason = "requeued during graceful shutdown"
        async with pool.acquire() as conn:
            async with conn.transaction():
                for _, (_, job_id) in tracked:
                    await conn.execute(
                        "UPDATE sync_jobs SET status='pending', error=$2, next_retry_at=NULL, "
                        "started_at=NULL, completed_at=NULL, updated_at=now() "
                        "WHERE id=$1 AND status IN ('pending', 'running')",
                        job_id, reason,
                    )
                    await conn.execute(
                        "UPDATE source_streams SET status='idle', last_error=$2, updated_at=now() "
                        "WHERE connection_id=(SELECT connection_id FROM sync_jobs WHERE id=$1) "
                        "AND status='syncing'",
                        job_id, reason,
                    )


async def recover_stuck_sync_jobs() -> int:
    """Requeue connector jobs left pending/running by a crashed process.

    The old implementation only recovered memory-formation rows. Because sync
    tasks were fire-and-forget, a process restart could leave a `running` job
    that permanently blocked the periodic resync safety net.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.SYNC_JOB_STALE_S)
    pool = await db.get_pool()
    recovered: List[Tuple[str, int]] = []
    reason = "recovered after an abandoned connector task"
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, provider, connection_id FROM sync_jobs "
                "WHERE status IN ('pending', 'running') "
                "AND COALESCE(updated_at, started_at, created_at) < $1 "
                "ORDER BY id FOR UPDATE SKIP LOCKED",
                cutoff,
            )
            for row in rows:
                await conn.execute(
                    "UPDATE sync_jobs SET status='pending', error=$2, next_retry_at=NULL, "
                    "started_at=NULL, completed_at=NULL, updated_at=now() WHERE id=$1",
                    row["id"], reason,
                )
                await conn.execute(
                    "UPDATE source_streams SET status='idle', last_error=$3, updated_at=now() "
                    "WHERE connection_id=$2 AND workspace_id=(SELECT workspace_id FROM sync_jobs WHERE id=$1) "
                    "AND status IN ('syncing', 'failed', 'paused')",
                    row["id"], row["connection_id"], reason,
                )
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    row["connection_id"], reason,
                )
                recovered.append((row["provider"], row["id"]))
    for provider, job_id in recovered:
        _dispatch_sync(provider, job_id)
    if recovered:
        log.warning("recovered %d abandoned connector job(s)", len(recovered))
    return len(recovered)


async def _run_then_chain(runner, provider: str, job_id: int) -> None:
    await runner(job_id)
    try:
        await _chain_full_backfill(job_id)
    except Exception:
        log.exception("failed to chain full backfill after sync job %d", job_id)


async def _chain_full_backfill(job_id: int) -> None:
    """If a just-completed fast-slice backfill asked for a follow-up, enqueue the
    full-history backfill now. Connectors ingest oldest-first, so the slice
    already surfaced recent memory; this deepens history in the background.
    Dedup (content hash) absorbs the overlap. The chained job carries no
    then_full_days, so it never chains again."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT id, workspace_id, connection_id, provider, status, state "
            "FROM sync_jobs WHERE id=$1",
            job_id,
        )
        if job is None or job["status"] != "complete":
            return  # only deepen after a clean slice; a failed slice is retried instead
        full_days = (job["state"] or {}).get("then_full_days")
        if not full_days:
            return
        active = await conn.fetchval(
            "SELECT 1 FROM sync_jobs WHERE connection_id=$1 "
            "AND status IN ('pending','running','paused') LIMIT 1",
            job["connection_id"],
        )
        if active:
            return
        new_id = await conn.fetchval(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
            "VALUES($1, $2, $3, 'pending', 'backfill', $4, $5) RETURNING id",
            job["workspace_id"], job["connection_id"], job["provider"],
            {"days": int(full_days)}, {"documents": 0, "duplicates": 0, "streams": 0},
        )
    log.info("chained full backfill job %d (%d days) after fast-slice %d",
             new_id, int(full_days), job_id)
    _dispatch_sync(job["provider"], new_id)


async def _enqueue_connect_resync(conn, connection) -> Optional[int]:
    """On (re)connect, kick a sync immediately if streams are already selected
    (the re-auth case) and nothing is running. First-time connects have nothing
    selected yet — resync_tick picks those up once the user chooses streams.
    Returns the job id to dispatch after the surrounding transaction commits,
    or None. Leaves the window unset so the connector derives it per stream."""
    selected = await conn.fetchval(
        "SELECT count(*) FROM source_streams WHERE connection_id=$1 AND selected",
        connection["id"],
    )
    active = await conn.fetchval(
        "SELECT 1 FROM sync_jobs WHERE connection_id=$1 "
        "AND status IN ('pending','running','paused') LIMIT 1",
        connection["id"],
    )
    if not selected or active:
        return None
    kind = "backfill" if connection["last_sync_at"] is None else "reconcile"
    job_id = await conn.fetchval(
        "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
        "VALUES($1, $2, $3, 'pending', $4, $5, $6) RETURNING id",
        connection["workspace_id"], connection["id"], connection["provider"], kind,
        {}, {"documents": 0, "duplicates": 0, "streams": 0},
    )
    await auth.audit(conn, "resync_start", connection["workspace_id"], connection["created_by"],
                     "sync_job", job_id,
                     {"connection_id": connection["id"], "kind": kind, "trigger": "reconnect"})
    return job_id


async def _refresh_slack_streams(conn, connection) -> List[Dict[str, Any]]:
    token = _decrypt_secret(connection["access_token_enc"])
    cursor = ""
    rows: List[Dict[str, Any]] = []
    while True:
        data = await _slack_api(
            token,
            "conversations.list",
            {
                "types": "public_channel",
                "exclude_archived": "true",
                "limit": 200,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        for ch in data.get("channels", []):
            stream = await conn.fetchrow(
                "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
                "name, metadata) VALUES($1, $2, 'slack', $3, $4, $5) "
                "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
                "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
                "updated_at=now() RETURNING id, external_id, name, selected, status, "
                "last_synced_at, last_error, metadata",
                connection["workspace_id"], connection["id"], ch["id"], ch.get("name", ch["id"]),
                {
                    "is_channel": ch.get("is_channel"),
                    "is_member": ch.get("is_member"),
                    "is_archived": ch.get("is_archived"),
                    "num_members": ch.get("num_members"),
                },
            )
            rows.append(dict(stream))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return rows


@router.get("/sources")
async def list_sources(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.id, c.provider, c.name, c.status, c.external_workspace_id, "
            "c.metadata, c.last_sync_at, c.last_error, c.created_at, c.updated_at, "
            "(SELECT count(*) FROM source_streams s WHERE s.connection_id=c.id) AS stream_count, "
            "(SELECT count(*) FROM source_streams s WHERE s.connection_id=c.id AND s.selected) AS selected_count, "
            "(SELECT count(*) FROM sync_jobs j WHERE j.connection_id=c.id AND j.status IN ('pending','running','paused')) AS active_jobs, "
            "(SELECT (j.stats->>'documents')::int FROM sync_jobs j WHERE j.connection_id=c.id "
            " AND j.status='complete' ORDER BY j.completed_at DESC NULLS LAST LIMIT 1) AS last_sync_documents "
            "FROM source_connections c WHERE c.workspace_id=$1 ORDER BY c.created_at DESC",
            current.workspace_id,
        )
    return {
        "configured": {
            "slack": slack_routes.configured(), "jira": jira.configured(),
            "github": github.configured(), "linear": linear.configured(),
            "confluence": confluence.configured(), "discord": discord.configured(),
            "googledocs": googledocs.configured(), "notion": notion.configured(),
            "figma": figma.configured(),
        },
        "connections": [dict(r) for r in rows],
    }


@router.get("/sources/{connection_id}/streams")
async def list_streams(
    connection_id: int,
    refresh: bool = True,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        connection = await _connection(conn, current.workspace_id, connection_id)
        if connection["provider"] == "slack" and refresh:
            try:
                await _refresh_slack_streams(conn, connection)
            except SlackRateLimit as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, f"Slack rate limited stream refresh; retry after {e.retry_after}s",
                )
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "jira" and refresh:
            try:
                token = await jira.valid_access_token(conn, connection)
                await jira.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "github" and refresh:
            try:
                token = _decrypt_secret(connection["access_token_enc"])
                await github.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "linear" and refresh:
            try:
                token = await linear.valid_access_token(conn, connection)
                await linear.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "confluence" and refresh:
            try:
                token = await confluence.valid_access_token(conn, connection)
                await confluence.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "discord" and refresh:
            try:
                token = _decrypt_secret(connection["access_token_enc"])
                await discord.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "googledocs" and refresh:
            try:
                token = await googledocs.valid_access_token(conn, connection)
                await googledocs.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "notion" and refresh:
            try:
                token = _decrypt_secret(connection["access_token_enc"])
                await notion.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        elif connection["provider"] == "figma" and refresh:
            try:
                token = await figma.valid_access_token(conn, connection)
                await figma.refresh_streams(conn, connection, token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
        rows = await conn.fetch(
            "SELECT id, external_id, name, selected, status, last_synced_at, last_error, metadata "
            "FROM source_streams WHERE workspace_id=$1 AND connection_id=$2 ORDER BY name",
            current.workspace_id, connection_id,
        )
    return [dict(r) for r in rows]


@router.patch("/sources/{connection_id}/streams/{stream_id}")
async def patch_stream(
    connection_id: int,
    stream_id: int,
    req: StreamPatch,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _connection(conn, current.workspace_id, connection_id)
            row = await conn.fetchrow(
                "UPDATE source_streams SET selected=$4, updated_at=now() "
                "WHERE id=$1 AND workspace_id=$2 AND connection_id=$3 "
                "RETURNING id, external_id, name, selected, status, last_synced_at, last_error, metadata",
                stream_id, current.workspace_id, connection_id, req.selected,
            )
            if row is None:
                raise HTTPException(404, "source stream not found")
            await auth.audit(conn, "source_stream_select", current.workspace_id,
                             current.user_id, "source_stream", stream_id,
                             {"selected": req.selected, "connection_id": connection_id})
    return dict(row)


@router.post("/sources/{connection_id}/sync")
async def start_sync(
    connection_id: int,
    req: SyncRequest,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    days = max(1, min(req.days, 180))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection = await _connection(conn, current.workspace_id, connection_id)
            selected = await conn.fetchval(
                "SELECT count(*) FROM source_streams "
                "WHERE workspace_id=$1 AND connection_id=$2 AND selected",
                current.workspace_id, connection_id,
            )
            if not selected:
                raise HTTPException(400, "select at least one channel or project before syncing")
            state: Dict[str, Any] = {"days": days}
            if req.then_full_days:
                state["then_full_days"] = max(1, min(req.then_full_days, 180))
            job = await conn.fetchrow(
                "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats, created_by) "
                "VALUES($1, $2, $3, 'pending', 'backfill', $4, $5, $6) "
                "RETURNING id, status, kind, state, stats, error, next_retry_at, created_at",
                current.workspace_id, connection_id, connection["provider"],
                state, {"documents": 0, "duplicates": 0, "streams": 0},
                current.user_id,
            )
            await auth.audit(conn, "sync_start", current.workspace_id, current.user_id,
                             "sync_job", job["id"], {"connection_id": connection_id, "days": days})
    _dispatch_sync(connection["provider"], job["id"])
    return dict(job)


@router.get("/sources/{connection_id}/jobs")
async def list_jobs(
    connection_id: int,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await _connection(conn, current.workspace_id, connection_id)
        rows = await conn.fetch(
            "SELECT id, status, kind, state, stats, error, next_retry_at, created_at, "
            "started_at, completed_at, updated_at FROM sync_jobs "
            "WHERE workspace_id=$1 AND connection_id=$2 ORDER BY created_at DESC LIMIT 20",
            current.workspace_id, connection_id,
        )
    return [dict(r) for r in rows]


@router.post("/sources/{connection_id}/jobs/{job_id}/retry")
async def retry_job(
    connection_id: int,
    job_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection = await _connection(conn, current.workspace_id, connection_id)
            job = await conn.fetchrow(
                "SELECT id, status FROM sync_jobs "
                "WHERE id=$1 AND workspace_id=$2 AND connection_id=$3",
                job_id, current.workspace_id, connection_id,
            )
            if job is None:
                raise HTTPException(404, "sync job not found")
            if job["status"] not in ("failed", "paused"):
                raise HTTPException(400, "only failed or paused sync jobs can be retried")
            row = await conn.fetchrow(
                "UPDATE sync_jobs SET status='pending', error=NULL, next_retry_at=NULL, "
                "started_at=NULL, completed_at=NULL, updated_at=now() "
                "WHERE id=$1 AND workspace_id=$2 AND connection_id=$3 "
                "RETURNING id, status, kind, state, stats, error, next_retry_at, created_at, "
                "started_at, completed_at, updated_at",
                job_id, current.workspace_id, connection_id,
            )
            await conn.execute(
                "UPDATE source_streams SET status='idle', last_error=NULL, updated_at=now() "
                "WHERE workspace_id=$1 AND connection_id=$2 AND status IN ('failed','paused')",
                current.workspace_id, connection_id,
            )
            await conn.execute(
                "UPDATE source_connections SET last_error=NULL, updated_at=now() "
                "WHERE id=$1 AND workspace_id=$2",
                connection_id, current.workspace_id,
            )
            await auth.audit(conn, "sync_retry", current.workspace_id, current.user_id,
                             "sync_job", job_id, {"connection_id": connection_id})
    _dispatch_sync(connection["provider"], job_id)
    return dict(row)


@router.delete("/sources/{connection_id}")
async def delete_source(
    connection_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _connection(conn, current.workspace_id, connection_id)
            deleted = await conn.fetchval(
                "DELETE FROM source_connections WHERE id=$1 AND workspace_id=$2 RETURNING id",
                connection_id, current.workspace_id,
            )
            await auth.audit(conn, "source_disconnect", current.workspace_id, current.user_id,
                             "source_connection", connection_id)
    return {"deleted": deleted}


def _message_ok(m: Dict[str, Any]) -> bool:
    if m.get("subtype") in {"channel_join", "channel_leave", "bot_message"}:
        return False
    return bool((m.get("text") or "").strip())


def _message_line(m: Dict[str, Any]) -> str:
    user = m.get("user") or m.get("username") or "unknown"
    return f"{user}: {slack.clean_text(m.get('text', ''))}"


def _ts_to_iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _day(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).date().isoformat()


async def _thread_doc(
    token: str, connection, stream, root: Dict[str, Any]
) -> Optional[IngestRequest]:
    channel = stream["external_id"]
    thread_ts = root.get("thread_ts") or root.get("ts")
    messages = [root]
    if int(root.get("reply_count") or 0) > 0:
        data = await _slack_api(
            token,
            "conversations.replies",
            {"channel": channel, "ts": thread_ts, "limit": 200},
        )
        messages = data.get("messages", messages)
    messages = [m for m in messages if _message_ok(m)]
    if not messages:
        return None
    text = "\n\n".join(_message_line(m) for m in messages)
    if len(text) < config.SLACK_MIN_THREAD_CHARS:
        return None
    head = slack.clean_text(messages[0].get("text", "")).split("\n")[0][:70]
    return IngestRequest(
        source="slack",
        title=f"#{stream['name']} thread: {head or thread_ts}",
        text=text,
        author=messages[0].get("user"),
        created_at=_ts_to_iso(messages[0].get("ts", thread_ts)),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        external_ref=f"slack:{connection['external_workspace_id']}:{channel}:{thread_ts}",
    )


def _digest_doc(connection, stream, day: str, messages: List[Dict[str, Any]]) -> Optional[IngestRequest]:
    messages = [m for m in messages if _message_ok(m)]
    if not messages:
        return None
    text = "\n\n".join(_message_line(m) for m in messages)
    if len(text) < max(config.SLACK_MIN_THREAD_CHARS, 400):
        return None
    return IngestRequest(
        source="slack",
        title=f"#{stream['name']} — {day} discussion",
        text=text,
        author=messages[0].get("user"),
        created_at=_ts_to_iso(messages[0].get("ts")),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        external_ref=f"slack:{connection['external_workspace_id']}:{stream['external_id']}:digest:{day}",
    )


async def _sync_stream(
    token: str, connection, stream, oldest: float, job_id: int
) -> Tuple[int, int]:
    cursor = ""
    messages: List[Dict[str, Any]] = []
    pool = await db.get_pool()
    while True:
        data = await _slack_api(
            token,
            "conversations.history",
            {
                "channel": stream["external_id"],
                "oldest": str(oldest),
                "limit": 200,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        messages.extend(data.get("messages", []))
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE source_streams SET sync_cursor=$2, updated_at=now() WHERE id=$1",
                stream["id"], {"history_cursor": cursor, "oldest": oldest},
            )
            await conn.execute(
                "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                job_id, {
                    "current_stream_id": stream["id"],
                    "current_stream": stream["name"],
                    "history_cursor": cursor,
                    "messages_seen": len(messages),
                },
            )
        if not cursor:
            break
    messages.sort(key=lambda m: float(m.get("ts", "0")))
    roots = [m for m in messages if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts")]
    docs: List[IngestRequest] = []
    loose: Dict[str, List[Dict[str, Any]]] = {}
    for m in roots:
        if int(m.get("reply_count") or 0) > 0:
            doc = await _thread_doc(token, connection, stream, m)
            if doc:
                docs.append(doc)
        else:
            loose.setdefault(_day(m["ts"]), []).append(m)
    for day, day_msgs in loose.items():
        doc = _digest_doc(connection, stream, day, day_msgs)
        if doc:
            docs.append(doc)
    docs.sort(key=lambda d: d.created_at or "")
    created = duplicate = 0
    for doc in docs:
        _, dup = await ingest_document(doc, workspace_id=connection["workspace_id"])
        if dup:
            duplicate += 1
        else:
            created += 1
    return created, duplicate


async def resync_tick() -> int:
    """Periodic re-sync safety net across all connectors. For each connected
    connection with selected streams and no active job whose last_sync_at is
    older than its provider's interval, enqueue a sync job. Dedup (content hash
    + external ref) absorbs the overlap, so re-fetching is free except for API
    calls. Jobs are normal sync jobs — visible in the jobs list, with the same
    rate-limit pausing. Called from the formation worker's idle tick.

    Slack (realtime via Events API) re-fetches a short SLACK_RECONCILE_WINDOW_DAYS
    window as a safety net for missed deliveries. Jira/GitHub have no realtime
    path, so the per-stream lookback is derived at run time by the connector
    (full backfill for never-synced streams, short window otherwise) — the tick
    leaves the job window unset and only decides when to fire."""
    await recover_stuck_sync_jobs()
    # (provider, interval_seconds, job_state, backfill_when_never)
    specs: List[tuple] = []
    if config.SLACK_RECONCILE_INTERVAL_S:
        specs.append(("slack", config.SLACK_RECONCILE_INTERVAL_S,
                      {"days": config.SLACK_RECONCILE_WINDOW_DAYS}, False))
    if config.CONNECTOR_RESYNC_INTERVAL_S:
        specs.append(("jira", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("github", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("linear", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("confluence", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("discord", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("googledocs", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("notion", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("figma", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
    if not specs:
        return 0
    pool = await db.get_pool()
    dispatched: List[tuple] = []
    async with pool.acquire() as conn:
        for provider, interval_s, base_state, backfill_first in specs:
            due = await conn.fetch(
                "SELECT c.id, c.workspace_id, c.last_sync_at, (c.last_sync_at IS NULL) AS never "
                "FROM source_connections c "
                "WHERE c.provider=$1 AND c.status='connected' "
                "AND c.access_token_enc IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM source_streams s "
                "            WHERE s.connection_id=c.id AND s.selected) "
                "AND NOT EXISTS (SELECT 1 FROM sync_jobs j WHERE j.connection_id=c.id "
                "                AND j.status IN ('pending','running','paused')) "
                "AND COALESCE(c.last_sync_at, 'epoch'::timestamptz) "
                "    < now() - ($2 || ' seconds')::interval",
                provider, str(interval_s),
            )
            for c in due:
                kind = "backfill" if (backfill_first and c["never"]) else "reconcile"
                state = base_state
                if provider == "slack":
                    # Widen the reconcile window to span the actual gap since the
                    # last sync, so an outage longer than SLACK_RECONCILE_WINDOW_DAYS
                    # doesn't drop messages permanently (dedup absorbs the overlap).
                    state = {"days": slack_reconcile_days(c["last_sync_at"])}
                job_id = await conn.fetchval(
                    "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, "
                    "state, stats) VALUES($1, $2, $3, 'pending', $4, $5, $6) RETURNING id",
                    c["workspace_id"], c["id"], provider, kind, state,
                    {"documents": 0, "duplicates": 0, "streams": 0},
                )
                await auth.audit(conn, "resync_start", c["workspace_id"], None,
                                 "sync_job", job_id, {"connection_id": c["id"], "kind": kind})
                dispatched.append((provider, job_id))
    for provider, job_id in dispatched:
        _dispatch_sync(provider, job_id)
    return len(dispatched)


# Back-compat alias: the formation worker and slack.sync re-export this name.
reconcile_tick = resync_tick


async def run_slack_sync_job(job_id: int) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM sync_jobs WHERE id=$1", job_id)
        if job is None:
            return
        connection = await conn.fetchrow(
            "SELECT * FROM source_connections WHERE id=$1", job["connection_id"]
        )
        if connection is None or connection["provider"] != "slack":
            return
        streams = await conn.fetch(
            "SELECT * FROM source_streams WHERE connection_id=$1 AND selected ORDER BY name",
            connection["id"],
        )
        await conn.execute(
            "UPDATE sync_jobs SET status='running', started_at=COALESCE(started_at, now()), "
            "updated_at=now() WHERE id=$1",
            job_id,
        )
    token = _decrypt_secret(connection["access_token_enc"])
    days = int((job["state"] or {}).get("days", 90))
    oldest = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    stats = {"documents": 0, "duplicates": 0, "streams": 0}
    current_stream_id: Optional[int] = None
    try:
        for stream in streams:
            current_stream_id = stream["id"]
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='syncing', last_error=NULL, updated_at=now() "
                    "WHERE id=$1",
                    stream["id"],
                )
                await conn.execute(
                    "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                    job_id, {"current_stream_id": stream["id"], "current_stream": stream["name"]},
                )
            created, duplicate = await _sync_stream(token, connection, stream, oldest, job_id)
            stats["documents"] += created
            stats["duplicates"] += duplicate
            stats["streams"] += 1
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='idle', last_synced_at=now(), "
                    "last_error=NULL, updated_at=now() WHERE id=$1",
                    stream["id"],
                )
                await conn.execute(
                    "UPDATE sync_jobs SET stats=$2, updated_at=now() WHERE id=$1",
                    job_id, stats,
                )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='complete', stats=$2, completed_at=now(), "
                "updated_at=now() WHERE id=$1",
                job_id, stats,
            )
            await conn.execute(
                "UPDATE source_connections SET last_sync_at=now(), last_error=NULL, updated_at=now() "
                "WHERE id=$1",
                connection["id"],
            )
            await auth.audit(conn, "sync_complete", connection["workspace_id"],
                             job["created_by"], "sync_job", job_id, stats)
    except SlackRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Slack rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1",
                    current_stream_id, f"Slack rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Slack rate limited sync; retry after {e.retry_after}s",
            )
            await auth.audit(conn, "sync_paused", connection["workspace_id"],
                             job["created_by"], "sync_job", job_id,
                             {"retry_after": e.retry_after, **stats})
    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='failed', stats=$2, error=$3, completed_at=now(), "
                "updated_at=now() WHERE id=$1",
                job_id, stats, str(e)[:1000],
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='failed', last_error=$2, updated_at=now() "
                    "WHERE id=$1",
                    current_stream_id, str(e)[:500],
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], str(e)[:500],
            )
            await auth.audit(conn, "sync_failed", connection["workspace_id"],
                             job["created_by"], "sync_job", job_id,
                             {"error": str(e)[:500], **stats})
