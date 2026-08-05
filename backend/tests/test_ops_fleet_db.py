"""Cross-workspace ops fleet view (/api/ops/fleet + /fleet/activity):
aggregates only workspaces where the caller is admin/owner, counts match
seeded state, and the activity feed rejects non-operated workspaces."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from app.domains.auth import service as auth

from test_api_endpoints import _transport


async def _multi_ws_client(pool, roles):
    """One user with the given role in a fresh workspace per entry.
    Returns (client, user_id, [workspace_ids])."""
    email = f"fleet-{uuid4().hex}@example.test"
    token = f"test-{uuid4().hex}"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) "
            "VALUES($1, 'Fleet User', $2) RETURNING id",
            email, await auth.hash_password("correct horse battery staple"))
        ws_ids = []
        for i, role in enumerate(roles):
            wid = await conn.fetchval(
                "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                f"Fleet WS {i}", f"fleet-{uuid4().hex[:10]}")
            await conn.execute(
                "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                "VALUES($1, $2, $3)", wid, user_id, role)
            ws_ids.append(wid)
        await conn.execute(
            "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, expires_at) "
            "VALUES($1, $2, $3, $4)",
            user_id, ws_ids[0], auth._hash_token(token),
            datetime.now(timezone.utc) + timedelta(days=1))
    client = httpx.AsyncClient(
        transport=_transport(), base_url="http://testserver",
        cookies={"sb_session": token})
    return client, user_id, ws_ids


async def _seed_workspace(conn, wid):
    await conn.execute(
        "INSERT INTO documents(workspace_id, source, title, raw_text, formation_status) "
        "VALUES ($1, 'meeting', 'queued 1', 'x', 'pending'), "
        "       ($1, 'meeting', 'queued 2', 'x', 'processing'), "
        "       ($1, 'meeting', 'broken', 'x', 'failed')", wid)
    await conn.execute(
        "INSERT INTO memory_proposals(workspace_id, kind, label, summary, topics) "
        "VALUES($1, 'decision', 'pending prop', 's', ARRAY['x'])", wid)
    await conn.execute(
        "INSERT INTO source_connections(workspace_id, provider, name, status, last_error) "
        "VALUES($1, 'slack', 'Slack', 'error', 'token expired')", wid)
    await conn.execute(
        "INSERT INTO usage_events(workspace_id, surface, kind, provider, model, "
        "total_tokens, request_count) VALUES($1, 'agent', 'llm', 'anthropic', 'm', 1234, 3)",
        wid)
    await conn.execute(
        "INSERT INTO formation_runs(workspace_id, document_id, status, duration_ms, started_at) "
        "SELECT $1, id, 'success', 2000, now() FROM documents WHERE workspace_id=$1 LIMIT 1",
        wid)


async def test_fleet_covers_only_operated_workspaces(pool, workspace_id):
    client, _, ws_ids = await _multi_ws_client(pool, ["owner", "admin", "member"])
    owned, admined, membered = ws_ids
    async with pool.acquire() as conn:
        await _seed_workspace(conn, owned)
    async with client:
        body = (await client.get("/api/ops/fleet")).json()
    by_id = {w["workspace_id"]: w for w in body["workspaces"]}
    assert set(by_id) == {owned, admined}  # member-only workspace excluded
    card = by_id[owned]
    assert card["is_active"] is True
    assert card["queue_depth"] == 2
    assert card["failed_docs"] == 1
    assert card["pending_proposals"] == 1
    assert card["failing_connectors"] == ["slack"]
    assert card["tokens_24h"] == 1234
    assert card["requests_24h"] == 3
    assert card["slo_24h"]["runs"] == 1
    assert card["slo_24h"]["p95_ms"] == 2000
    # empty workspace still gets a zeroed card
    assert by_id[admined]["queue_depth"] == 0
    assert by_id[admined]["connectors"] == []


async def test_fleet_activity_scoping(pool, workspace_id):
    client, user_id, ws_ids = await _multi_ws_client(pool, ["owner", "member"])
    owned, membered = ws_ids
    async with pool.acquire() as conn:
        await auth.audit(conn, "memory_edit", owned, user_id, "memory_node", 1, {})
        await auth.audit(conn, "memory_edit", membered, user_id, "memory_node", 2, {})
    async with client:
        feed = (await client.get("/api/ops/fleet/activity")).json()
        scoped = (await client.get(f"/api/ops/fleet/activity?workspace_id={owned}")).json()
        denied = await client.get(f"/api/ops/fleet/activity?workspace_id={membered}")
    assert {e["workspace_id"] for e in feed["events"]} == {owned}
    assert all(e["workspace_name"] == "Fleet WS 0" for e in feed["events"])
    assert {e["workspace_id"] for e in scoped["events"]} == {owned}
    assert denied.status_code == 403  # member of it, but not an operator
