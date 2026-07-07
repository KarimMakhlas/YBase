"""Discord OAuth (bot install) + callback routes.

Unlike the 3-legged connectors, the OAuth flow here only decides which guild
the bot gets installed into — the credential that actually reads messages is
the static DISCORD_BOT_TOKEN from config, stored per-connection at install
time for uniformity with every other connector's token handling.
"""

import secrets
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core import config, db
from app.core.crypto import encrypt_secret as _encrypt_secret
from app.domains.auth import service as auth
from app.domains.connectors.discord import client as discord

router = APIRouter(tags=["sources"])


@router.get("/sources/discord/install-url")
async def discord_install_url(
    request: Request,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    from app.domains.connectors import service  # lazy: service imports this router

    if not discord.configured():
        return {
            "configured": False,
            "error": "Discord requires DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_BOT_TOKEN, and CONNECTOR_SECRET_KEY.",
        }
    state = secrets.token_urlsafe(32)
    return_to = service._frontend_from_request(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_states(state, workspace_id, user_id, provider, redirect_path, expires_at) "
            "VALUES($1, $2, $3, 'discord', $4, now() + interval '10 minutes')",
            state, current.workspace_id, current.user_id, return_to,
        )
    return {"configured": True, "url": discord.authorize_url(state)}


@router.get("/integrations/discord/oauth/callback")
async def discord_oauth_callback(code: str = "", state: str = ""):
    from app.domains.connectors import service  # lazy: service imports this router

    if not code or not state:
        raise HTTPException(400, "missing code or state")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        oauth_state = await conn.fetchrow(
            "UPDATE oauth_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='discord' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING workspace_id, user_id, redirect_path",
            state,
        )
        if oauth_state is None:
            raise HTTPException(400, "invalid or expired oauth state")
    try:
        payload = await discord.exchange_code(code)
        guild = payload.get("guild") or {}
        guild_id = guild.get("id")
        if not guild_id:
            raise discord.DiscordAPIError("no guild in oauth response")
        guild_name = guild.get("name") or "Discord"
    except Exception:
        redirect = (oauth_state["redirect_path"] or "/") + "?discord=error"
        return RedirectResponse(redirect)

    bot_token = config.DISCORD_BOT_TOKEN
    async with pool.acquire() as conn:
        async with conn.transaction():
            connection_id = await conn.fetchval(
                "INSERT INTO source_connections(workspace_id, provider, name, status, "
                "external_workspace_id, access_token_enc, metadata, created_by) "
                "VALUES($1, 'discord', $2, 'connected', $3, $4, $5, $6) "
                "ON CONFLICT (workspace_id, provider, external_workspace_id) DO UPDATE SET "
                "name=EXCLUDED.name, status='connected', access_token_enc=EXCLUDED.access_token_enc, "
                "metadata=source_connections.metadata || EXCLUDED.metadata, last_error=NULL, updated_at=now() "
                "RETURNING id",
                oauth_state["workspace_id"], guild_name, guild_id, _encrypt_secret(bot_token),
                {"guild": {"id": guild_id, "name": guild_name}}, oauth_state["user_id"],
            )
            await auth.audit(conn, "discord_install", oauth_state["workspace_id"],
                             oauth_state["user_id"], "source_connection", connection_id,
                             {"guild_id": guild_id, "guild_name": guild_name})
            connection = await conn.fetchrow(
                "SELECT * FROM source_connections WHERE id=$1", connection_id
            )
            try:
                await discord.refresh_streams(conn, connection, bot_token)
            except Exception as e:  # channels can be fetched later via Refresh
                await conn.execute(
                    "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                    connection_id, str(e)[:500],
                )
            resync_job_id = await service._enqueue_connect_resync(conn, connection)
    if resync_job_id:
        service._dispatch_sync("discord", resync_job_id)
    redirect = (oauth_state["redirect_path"] or "/") + "?discord=connected"
    return RedirectResponse(redirect)
