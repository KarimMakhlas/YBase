"""Shared Atlassian Cloud OAuth 3LO machinery (Jira + Confluence).

Both products use the same authorize/token endpoints and the same
accessible-resources call to resolve the cloud id — only the client
credentials, scopes, and per-product API paths differ. Callers pass their own
client_id/client_secret so Jira and Confluence stay separate OAuth apps and
separate source_connections (decided during connector-expansion planning).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlencode

import httpx

from app.core import crypto

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API_BASE = "https://api.atlassian.com"


def authorize_url(client_id: str, scopes: str, redirect_uri: str, state: str) -> str:
    params = urlencode({
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    })
    return f"{AUTHORIZE_URL}?{params}"


async def _token_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(TOKEN_URL, json=payload)
    res.raise_for_status()
    return res.json()


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    })


async def refresh_tokens(client_id: str, client_secret: str, refresh_token: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })


async def accessible_resources(access_token: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.get(
            f"{API_BASE}/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    res.raise_for_status()
    return res.json()


async def valid_access_token(
    conn, connection, client_id: str, client_secret: str, error_cls: type,
    reconnect_hint: str,
) -> str:
    """Return a non-expired access token for the connection, refreshing and
    persisting the rotating refresh token when the current one is near expiry."""
    expires = connection["token_expires_at"]
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    if connection["access_token_enc"] and expires is not None and expires > soon:
        return crypto.decrypt_secret(connection["access_token_enc"])
    if not connection["refresh_token_enc"]:
        raise error_cls(f"missing refresh token; reconnect {reconnect_hint}")
    payload = await refresh_tokens(
        client_id, client_secret, crypto.decrypt_secret(connection["refresh_token_enc"])
    )
    access = payload["access_token"]
    new_refresh = payload.get("refresh_token") or crypto.decrypt_secret(connection["refresh_token_enc"])
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    await conn.execute(
        "UPDATE source_connections SET access_token_enc=$2, refresh_token_enc=$3, "
        "token_expires_at=$4, updated_at=now() WHERE id=$1",
        connection["id"], crypto.encrypt_secret(access),
        crypto.encrypt_secret(new_refresh), new_expiry,
    )
    return access
