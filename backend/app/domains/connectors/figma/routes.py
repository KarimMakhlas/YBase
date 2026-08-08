"""Figma OAuth install + callback routes, plus the manual set-team route.

Figma has no API to list a user's teams, so connecting is a two-step flow:
OAuth first (identity + token), then the user pastes their team id from the
Figma URL (figma.com/files/team/<id>/...) into the UI, which calls the
set-team route here to store it and discover the team's projects as streams.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core import db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.figma import client as figma

router = APIRouter(tags=["sources"])


class TeamRequest(BaseModel):
    team_id: str


@router.get("/sources/figma/install-url")
async def figma_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not figma.configured():
        return {
            "configured": False,
            "error": "Figma OAuth requires FIGMA_CLIENT_ID, FIGMA_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'figma', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": figma.authorize_url(state)}


@router.get("/integrations/figma/oauth/callback")
async def figma_oauth_callback(code: str = "", state: str = ""):
    from app.domains.connectors import service  # lazy: service imports this router

    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='figma' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await figma.exchange_code(code)
        access_token = tokens["access_token"]
        account = await figma.me(access_token)
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?figma=error"
        return RedirectResponse(redirect)

    account_id = str(account.get("id") or tokens.get("user_id") or "")
    account_name = account.get("handle") or account.get("email") or "Figma"
    if not account_id:
        redirect = (oauth_state["redirect_path"] or "/") + "?figma=error"
        return RedirectResponse(redirect)
    refresh_token = tokens.get("refresh_token")
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 7776000))
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, refresh_token_enc, token_expires_at, "
                "metadata, created_by) "
                "VALUES($1, 'figma', $2, 'connected', $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, source_connections.refresh_token_enc), "
                "token_expires_at=EXCLUDED.token_expires_at, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], account_name, account_id,
                _encrypt_secret(access_token),
                _encrypt_secret(refresh_token) if refresh_token else None, expires_at,
                {"account": {"handle": account.get("handle"), "email": account.get("email")}},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "figma_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"handle": account.get("handle")})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            # No streams yet — Figma can't list teams, so the UI prompts for a
            # team id next and set_figma_team discovers the projects.
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("figma", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?figma=connected"
    return RedirectResponse(redirect)


@router.post("/sources/{connection_id}/figma/team")
async def set_figma_team(
    connection_id: int,
    req: TeamRequest,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> List[Dict[str, Any]]:
    """Store the manually-entered team id and discover its projects as streams.
    Accepts a bare id or a pasted Figma URL containing /team/<id>/."""
    from app.domains.connectors import service  # lazy: service imports this router

    raw = req.team_id.strip()
    m = re.search(r"/team/(\d+)", raw)
    team_id = m.group(1) if m else raw
    if not team_id.isdigit():
        raise HTTPException(400, "team id must be numeric — copy it from your Figma team URL")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection = await service._connection(conn, current.workspace_id, connection_id)
            if connection["provider"] != "figma":
                raise HTTPException(400, "not a Figma connection")
            token = await figma.valid_access_token(conn, connection)
            try:
                await conn.execute(
                    "UPDATE source_connections SET metadata = metadata || $2::jsonb, "
                    "updated_at=now() WHERE id=$1",
                    connection_id, {"team_id": team_id},
                )
                connection = await conn.fetchrow(
                    "SELECT * FROM source_connections WHERE id=$1", connection_id
                )
                streams = await figma.refresh_streams(conn, connection, token)
            except figma.FigmaAPIError as e:
                raise HTTPException(400, f"could not read that team: {str(e)[:200]}")
            await auth.audit(conn, "figma_team_set", current.workspace_id, current.user_id,
                             "source_connection", connection_id, {"team_id": team_id})
    return streams
