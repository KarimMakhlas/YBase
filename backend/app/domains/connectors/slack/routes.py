"""Slack OAuth install + callback routes.

Split out of connectors/service.py so new connectors don't keep growing that
file's route surface. The Slack sync engine itself (stream refresh, message
digesting, run_slack_sync_job) stays in service.py — it's tangled with the
shared _slack_api/_slack_post helpers used by both OAuth and sync, and moving
it isn't needed to keep this file's job (route wiring) separate from that.
"""

import secrets
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import config, db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth

router = APIRouter(tags=["sources"])

SLACK_SCOPES = "channels:read,channels:history"


def configured() -> bool:
    return bool(
        config.SLACK_CLIENT_ID
        and config.SLACK_CLIENT_SECRET
        and config.SLACK_SIGNING_SECRET
        and config.CONNECTOR_SECRET_KEY
    )


def _redirect_uri() -> str:
    return config.SLACK_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/slack/oauth/callback"


@router.get("/sources/slack/install-url")
async def slack_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not configured():
        return {
            "configured": False,
            "error": "Slack OAuth requires SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_SIGNING_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
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
    from app.domains.connectors import service  # lazy: service imports this router

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
        payload = await service._slack_post(
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
            await service._refresh_slack_streams(conn, connection)
    redirect = (oauth_state["redirect_path"] or "/") + "?slack=connected"
    return RedirectResponse(redirect)
