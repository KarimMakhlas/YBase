"""Linear OAuth install + callback routes.

Follows jira/routes.py's shape, minus the accessible-resources/cloud-id step —
a Linear access token is already scoped to one workspace, so the callback
resolves the organization identity via a GraphQL viewer query instead.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.linear import client as linear

router = APIRouter(tags=["sources"])


@router.get("/sources/linear/install-url")
async def linear_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not linear.configured():
        return {
            "configured": False,
            "error": "Linear OAuth requires LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'linear', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": linear.authorize_url(state)}


@router.get("/integrations/linear/oauth/callback")
async def linear_oauth_callback(code: str = "", state: str = ""):
    from app.domains.connectors import service  # lazy: service imports this router

    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='linear' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        tokens = await linear.exchange_code(code)
        access_token = tokens["access_token"]
        org = await linear.viewer_organization(access_token)
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?linear=error"
        return RedirectResponse(redirect)
    if not org or not org.get("id"):
        redirect = (oauth_state["redirect_path"] or "/") + "?linear=error"
        return RedirectResponse(redirect)

    org_id = org["id"]
    org_name = org.get("name") or "Linear"
    refresh_token = tokens.get("refresh_token")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 86400)))

    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, refresh_token_enc, token_expires_at, "
                "metadata, created_by) "
                "VALUES($1, 'linear', $2, 'connected', $3, $4, $5, $6, $7, $8) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "refresh_token_enc=EXCLUDED.refresh_token_enc, token_expires_at=EXCLUDED.token_expires_at, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], org_name, org_id, _encrypt_secret(access_token),
                _encrypt_secret(refresh_token) if refresh_token else None, expires_at,
                {"organization": {"id": org_id, "name": org_name}},
                oauth_state["user_id"],
            )
            await auth.audit(conn, "linear_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"organization_id": org_id, "organization_name": org_name})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await linear.refresh_streams(conn, connection, access_token)
            except Exception as e:  # teams can be fetched later via Refresh
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("linear", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?linear=connected"
    return RedirectResponse(redirect)
