"""Notion OAuth install + callback routes.

Notion's token exchange authenticates with HTTP Basic and returns the
workspace identity inline (workspace_id/workspace_name), so the callback
needs no follow-up identity call. Tokens don't expire — no refresh token,
token_expires_at stays NULL.
"""

import secrets
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.notion import client as notion

router = APIRouter(tags=["sources"])


@router.get("/sources/notion/install-url")
async def notion_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not notion.configured():
        return {
            "configured": False,
            "error": "Notion OAuth requires NOTION_CLIENT_ID, NOTION_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'notion', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": notion.authorize_url(state)}


@router.get("/integrations/notion/oauth/callback")
async def notion_oauth_callback(code: str = "", state: str = ""):
    from app.domains.connectors import service  # lazy: service imports this router

    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='notion' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await notion.exchange_code(code)
        access_token = tokens["access_token"]
        ws_id = tokens.get("workspace_id")
        if not ws_id:
            raise notion.NotionAPIError("no workspace_id in oauth response")
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?notion=error"
        return RedirectResponse(redirect)

    ws_name = tokens.get("workspace_name") or "Notion"
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, metadata, created_by) "
                "VALUES($1, 'notion', $2, 'connected', $3, $4, $5, $6) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], ws_name, ws_id, _encrypt_secret(access_token),
                {"workspace": {"id": ws_id, "name": ws_name}, "bot_id": tokens.get("bot_id")},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "notion_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"notion_workspace_id": ws_id, "notion_workspace_name": ws_name})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await notion.refresh_streams(conn, connection, access_token)
            except Exception as e:  # shared pages can be fetched later via Refresh
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("notion", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?notion=connected"
    return RedirectResponse(redirect)
