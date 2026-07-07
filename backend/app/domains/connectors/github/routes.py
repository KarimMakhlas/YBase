"""GitHub OAuth install + callback routes.

Split out of connectors/service.py so new connectors don't keep growing that
file's route surface. GitHub's client (token exchange, repo/issue sync) stays
self-contained in github/client.py; this module only wires the HTTP endpoints.
"""

import secrets
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.github import client as github

router = APIRouter(tags=["sources"])


@router.get("/sources/github/install-url")
async def github_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not github.configured():
        return {
            "configured": False,
            "error": "GitHub OAuth requires GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
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
    from app.domains.connectors import service  # lazy: service imports this router

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
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("github", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?github=connected"
    return RedirectResponse(redirect)
