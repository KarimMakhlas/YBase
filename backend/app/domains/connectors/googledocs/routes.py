"""Google Docs OAuth install + callback routes.

Standard Google OAuth2 with offline access. The callback resolves the account
identity via Drive's about endpoint (permission id = external_workspace_id)
and creates the single implicit "All Google Docs" stream.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.googledocs import client as googledocs

router = APIRouter(tags=["sources"])


@router.get("/sources/googledocs/install-url")
async def googledocs_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not googledocs.configured():
        return {
            "configured": False,
            "error": "Google Docs OAuth requires GOOGLE_DOCS_CLIENT_ID, GOOGLE_DOCS_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'googledocs', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": googledocs.authorize_url(state)}


@router.get("/integrations/googledocs/oauth/callback")
async def googledocs_oauth_callback(code: str = "", state: str = ""):
    from app.domains.connectors import service  # lazy: service imports this router

    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='googledocs' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await googledocs.exchange_code(code)
        access_token = tokens["access_token"]
        user = await googledocs.drive_user(access_token)
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?googledocs=error"
        return RedirectResponse(redirect)

    account_id = user.get("permissionId") or user.get("emailAddress")
    account_name = user.get("emailAddress") or user.get("displayName") or "Google Docs"
    if not account_id:
        redirect = (oauth_state["redirect_path"] or "/") + "?googledocs=error"
        return RedirectResponse(redirect)
    refresh_token = tokens.get("refresh_token")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 3600)))

    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, refresh_token_enc, token_expires_at, "
                "metadata, created_by) "
                "VALUES($1, 'googledocs', $2, 'connected', $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, source_connections.refresh_token_enc), "
                "token_expires_at=EXCLUDED.token_expires_at, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], account_name, str(account_id),
                _encrypt_secret(access_token),
                _encrypt_secret(refresh_token) if refresh_token else None, expires_at,
                {"user": {"email": user.get("emailAddress"), "name": user.get("displayName")}},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "googledocs_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"email": user.get("emailAddress")})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await googledocs.refresh_streams(conn, connection, access_token)
            except Exception as e:
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("googledocs", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?googledocs=connected"
    return RedirectResponse(redirect)
