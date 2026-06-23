"""Source connector APIs and Slack sync jobs."""

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core import config, db
from app.core.crypto import decrypt_secret as _decrypt_secret, encrypt_secret as _encrypt_secret
from app.domains.connectors import slack_reconcile_days
from app.domains.auth import service as auth
from app.domains.connectors.github import client as github
from app.domains.connectors.jira import client as jira
from app.domains.connectors.slack import events as slack
from app.domains.documents.ingestion import IngestRequest, ingest_document

router = APIRouter(prefix="/api", tags=["sources"])

log = logging.getLogger("ybase.connectors")

SLACK_SCOPES = "channels:read,channels:history"
SLACK_API = "https://slack.com/api"


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


def _redirect_uri() -> str:
    return config.SLACK_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/slack/oauth/callback"


def _frontend_from_request(request: Request) -> str:
    ref = request.headers.get("referer") or "http://localhost:5173/"
    return ref.split("?")[0]


def _slack_configured() -> bool:
    return bool(
        config.SLACK_CLIENT_ID
        and config.SLACK_CLIENT_SECRET
        and config.SLACK_SIGNING_SECRET
        and config.CONNECTOR_SECRET_KEY
    )


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
    runner = {"jira": jira.run_sync_job, "github": github.run_sync_job}.get(
        provider, run_slack_sync_job
    )
    asyncio.create_task(_run_then_chain(runner, provider, job_id))


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
        "configured": {"slack": _slack_configured(), "jira": jira.configured(),
                       "github": github.configured()},
        "connections": [dict(r) for r in rows],
    }


@router.get("/sources/slack/install-url")
async def slack_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    if not _slack_configured():
        return {
            "configured": False,
            "error": "Slack OAuth requires SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = _frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'slack', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    params = urlencode({
        "client_id": config.SLACK_CLIENT_ID,
        "scope": SLACK_SCOPES,
        "redirect_uri": _redirect_uri(),
        "state": state,
    })
    return {"configured": True, "url": f"https://slack.com/oauth/v2/authorize?{params}"}


@router.get("/integrations/slack/oauth/callback")
async def slack_oauth_callback(code: str = "", state: str = ""):
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='slack' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        payload = await _slack_post(
            "oauth.v2.access",
            {
                "client_id": config.SLACK_CLIENT_ID,
                "client_secret": config.SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
        )
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?slack=error"
        return RedirectResponse(redirect)

    access_token = payload.get("access_token") or payload.get("bot", {}).get("bot_access_token")
    team = payload.get("team") or {}
    team_id = team.get("id")
    team_name = team.get("name") or "Slack"
    if not access_token or not team_id:
        raise HTTPException(400, "Slack did not return a bot token and team id")

    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, bot_user_id, metadata, created_by) "
                "VALUES($1, 'slack', $2, 'connected', $3, $4, $5, $6, $7) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "bot_user_id=EXCLUDED.bot_user_id, metadata=source_connections.metadata || EXCLUDED.metadata, "
                "last_error=NULL, updated_at=now() RETURNING id",
                oauth_state["workspace_id"], team_name, team_id, _encrypt_secret(access_token),
                payload.get("bot_user_id"), {"team": team, "scope": payload.get("scope")},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "slack_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"team_id": team_id, "team_name": team_name})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            await _refresh_slack_streams(conn, connection)
    redirect = (oauth_state["redirect_path"] or "/") + "?slack=connected"
    return RedirectResponse(redirect)


@router.get("/sources/jira/install-url")
async def jira_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    if not jira.configured():
        return {
            "configured": False,
            "error": "Jira OAuth requires JIRA_CLIENT_ID, JIRA_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = _frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'jira', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": jira.authorize_url(state)}


@router.get("/integrations/jira/oauth/callback")
async def jira_oauth_callback(code: str = "", state: str = ""):
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='jira' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await jira.exchange_code(code)
        access_token = tokens["access_token"]
        resources = await jira.accessible_resources(access_token)
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?jira=error"
        return RedirectResponse(redirect)
    if not resources:
        redirect = (oauth_state["redirect_path"] or "/") + "?jira=error"
        return RedirectResponse(redirect)

    site = resources[0]
    cloud_id = site["id"]
    site_name = site.get("name") or site.get("url") or "Jira"
    refresh_token = tokens.get("refresh_token")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))

    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, refresh_token_enc, token_expires_at, "
                "metadata, created_by) "
                "VALUES($1, 'jira', $2, 'connected', $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "refresh_token_enc=EXCLUDED.refresh_token_enc, token_expires_at=EXCLUDED.token_expires_at, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], site_name, cloud_id, _encrypt_secret(access_token),
                _encrypt_secret(refresh_token) if refresh_token else None, expires_at,
                {"site": {"url": site.get("url"), "scopes": site.get("scopes")}},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "jira_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"cloud_id": cloud_id, "site_name": site_name})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await jira.refresh_streams(conn, connection, access_token)
            except Exception as e:  # projects can be fetched later via Refresh
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await _enqueue_connect_resync(conn, connection)
    if resync_job_id:
        _dispatch_sync("jira", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?jira=connected"
    return RedirectResponse(redirect)


@router.get("/sources/github/install-url")
async def github_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    if not github.configured():
        return {
            "configured": False,
            "error": "GitHub OAuth requires GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = _frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'github', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": github.authorize_url(state)}


@router.get("/integrations/github/oauth/callback")
async def github_oauth_callback(code: str = "", state: str = ""):
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='github' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await github.exchange_code(code)
        access_token = tokens["access_token"]
        acct = await github.account(access_token)
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?github=error"
        return RedirectResponse(redirect)

    account_id = str(acct.get("id"))
    login = acct.get("login") or "GitHub"
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, metadata, created_by) "
                "VALUES($1, 'github', $2, 'connected', $3, $4, $5, $6) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], login, account_id, _encrypt_secret(access_token),
                {"login": login, "scope": tokens.get("scope")}, oauth_state["user_id"],
            )
            await auth.audit(conn, "github_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"login": login})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await github.refresh_streams(conn, connection, access_token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await _enqueue_connect_resync(conn, connection)
    if resync_job_id:
        _dispatch_sync("github", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?github=connected"
    return RedirectResponse(redirect)


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
    # (provider, interval_seconds, job_state, backfill_when_never)
    specs: List[tuple] = []
    if config.SLACK_RECONCILE_INTERVAL_S:
        specs.append(("slack", config.SLACK_RECONCILE_INTERVAL_S,
                      {"days": config.SLACK_RECONCILE_WINDOW_DAYS}, False))
    if config.CONNECTOR_RESYNC_INTERVAL_S:
        specs.append(("jira", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
        specs.append(("github", config.CONNECTOR_RESYNC_INTERVAL_S, {}, True))
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
