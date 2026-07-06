"""Workspace API keys: mint → use → list → revoke lifecycle, auth failures,
and audit trail. The key is the only credential the agent API accepts."""

import httpx

from app.domains.auth import service as auth

from test_api_endpoints import _auth_client, _transport


async def _agent_client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport(), base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _mint_key(pool, workspace_id, name="ci-key"):
    client, user_id = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.post("/api/workspace/api-keys", json={"name": name})
    assert resp.status_code == 200
    return resp.json(), user_id


async def test_key_lifecycle(pool, workspace_id):
    created, user_id = await _mint_key(pool, workspace_id)
    token = created["token"]
    assert token.startswith(auth.API_KEY_PREFIX)
    assert created["token_prefix"] == token[:12]

    # plaintext never stored; only the hash is
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM api_keys WHERE id=$1", created["id"])
        audit_actions = {r["action"] for r in await conn.fetch(
            "SELECT action FROM audit_events WHERE workspace_id=$1", workspace_id)}
    assert row["token_hash"] != token
    assert token not in str(dict(row))
    assert row["created_by"] == user_id
    assert "api_key_created" in audit_actions

    # the key authenticates the agent API
    async with await _agent_client(token) as agent:
        resp = await agent.get("/api/agent/search?q=anything")
    assert resp.status_code == 200

    # list shows prefix, never the token
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        listing = await admin.get("/api/workspace/api-keys")
        assert listing.status_code == 200
        keys = listing.json()
        assert any(k["token_prefix"] == token[:12] for k in keys)
        assert all("token" not in k for k in keys)

        # revoke
        revoked = await admin.delete(f"/api/workspace/api-keys/{created['id']}")
        assert revoked.status_code == 200

    # revoked key stops working immediately
    async with await _agent_client(token) as agent:
        resp = await agent.get("/api/agent/search?q=anything")
    assert resp.status_code == 401

    async with pool.acquire() as conn:
        audit_actions = {r["action"] for r in await conn.fetch(
            "SELECT action FROM audit_events WHERE workspace_id=$1", workspace_id)}
    assert "api_key_revoked" in audit_actions


async def test_agent_api_rejects_bad_credentials(pool, workspace_id):
    for headers in (
        {},  # no header at all
        {"Authorization": "Bearer not-a-ybase-key"},
        {"Authorization": f"Bearer {auth.API_KEY_PREFIX}{'0' * 40}"},  # unknown
        {"Authorization": auth.generate_api_key()},  # missing Bearer scheme
    ):
        async with httpx.AsyncClient(
            transport=_transport(), base_url="http://testserver", headers=headers
        ) as client:
            resp = await client.get("/api/agent/search?q=x")
        assert resp.status_code == 401, headers


async def test_key_management_requires_admin(pool, workspace_id):
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        assert (await member.post("/api/workspace/api-keys",
                                  json={"name": "nope"})).status_code == 403
        assert (await member.get("/api/workspace/api-keys")).status_code == 403


async def test_key_updates_last_used(pool, workspace_id):
    created, _ = await _mint_key(pool, workspace_id)
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT last_used_at FROM api_keys WHERE id=$1", created["id"]) is None
    async with await _agent_client(created["token"]) as agent:
        await agent.get("/api/agent/search?q=x")
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT last_used_at FROM api_keys WHERE id=$1", created["id"]) is not None
