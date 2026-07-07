"""Topic-scoped API keys: a key with allowed_topics only sees memory linked
('about' edges) to those topics — across search, decision detail, and
propose — while NULL-scoped keys behave exactly as before."""

import httpx

from app.domains.memory import graph

from test_api_endpoints import _auth_client, _transport


async def _scoped_client(pool, workspace_id, allowed_topics):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        resp = await admin.post(
            "/api/workspace/api-keys",
            json={"name": "scoped", "allowed_topics": allowed_topics})
    body = resp.json()
    client = httpx.AsyncClient(
        transport=_transport(), base_url="http://testserver",
        headers={"Authorization": f"Bearer {body['token']}"},
    )
    return client, body


async def _seed_two_areas(pool, workspace_id):
    """billing decision (+ Alice via involves) and a hiring decision."""
    async with pool.acquire() as conn:
        billing_topic = await graph.upsert_node(conn, workspace_id, "topic", "billing")
        hiring_topic = await graph.upsert_node(conn, workspace_id, "topic", "hiring")
        billing = await graph.upsert_node(
            conn, workspace_id, "decision", "Retry failed charges three times",
            summary="Billing retry policy.", status="decided")
        hiring = await graph.upsert_node(
            conn, workspace_id, "decision", "Hire two backend engineers",
            summary="Hiring plan.", status="decided")
        alice = await graph.upsert_node(
            conn, workspace_id, "entity", "Alice", data={"entity_kind": "person"})
        await graph.add_edge(conn, workspace_id, billing, billing_topic, "about")
        await graph.add_edge(conn, workspace_id, hiring, hiring_topic, "about")
        await graph.add_edge(conn, workspace_id, billing, alice, "involves")
    return {"billing": billing, "hiring": hiring, "alice": alice,
            "billing_topic": billing_topic, "hiring_topic": hiring_topic}


async def test_scoped_search_filters_to_topic(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    agent, key = await _scoped_client(pool, workspace_id, ["Billing"])  # case-insensitive
    assert key["allowed_topics"] == ["billing"]
    async with agent:
        billing_hits = (await agent.get("/api/agent/search?q=Retry failed")).json()
        hiring_hits = (await agent.get("/api/agent/search?q=Hire two")).json()
    assert ids["billing"] in {h["id"] for h in billing_hits}
    assert hiring_hits == []  # in-workspace but out-of-scope


async def test_scoped_decision_detail_404s_out_of_scope(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    agent, _ = await _scoped_client(pool, workspace_id, ["billing"])
    async with agent:
        ok = await agent.get(f"/api/agent/decisions/{ids['billing']}")
        hidden = await agent.get(f"/api/agent/decisions/{ids['hiring']}")
    assert ok.status_code == 200
    assert hidden.status_code == 404  # indistinguishable from nonexistent


async def test_scoped_detail_filters_out_of_scope_relations(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    async with pool.acquire() as conn:
        await graph.add_edge(conn, workspace_id, ids["billing"], ids["hiring"], "relates_to")
    agent, _ = await _scoped_client(pool, workspace_id, ["billing"])
    async with agent:
        detail = (await agent.get(f"/api/agent/decisions/{ids['billing']}")).json()
    related_ids = {r["node_id"] for r in detail["related"]}
    assert ids["hiring"] not in related_ids  # reachable edge must not leak the node


async def test_scoped_propose_rejects_outside_topics(pool, workspace_id):
    await _seed_two_areas(pool, workspace_id)
    agent, _ = await _scoped_client(pool, workspace_id, ["billing"])
    body = {"label": "New billing decision", "summary": "s", "topics": ["billing"]}
    async with agent:
        ok = await agent.post("/api/agent/propose", json=body)
        outside = await agent.post(
            "/api/agent/propose",
            json={**body, "label": "Sneaky hiring decision", "topics": ["billing", "hiring"]})
    assert ok.status_code == 200
    assert outside.status_code == 403


async def test_unscoped_key_unchanged(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    agent, key = await _scoped_client(pool, workspace_id, None)
    assert key["allowed_topics"] is None
    async with agent:
        billing_hits = (await agent.get("/api/agent/search?q=Retry failed")).json()
        hiring_hits = (await agent.get("/api/agent/search?q=Hire two")).json()
        detail = await agent.get(f"/api/agent/decisions/{ids['hiring']}")
    assert ids["billing"] in {h["id"] for h in billing_hits}
    assert ids["hiring"] in {h["id"] for h in hiring_hits}
    assert detail.status_code == 200


async def test_scoped_context_briefing_filters_nodes(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    agent, _ = await _scoped_client(pool, workspace_id, ["billing"])
    async with agent:
        resp = await agent.post(
            "/api/agent/context",
            json={"task": "review the decided policies", "topics": ["billing", "hiring"]})
    body = resp.json()
    decision_ids = {d["node_id"] for d in body["relevant_decisions"]}
    assert ids["hiring"] not in decision_ids
    assert body["trace"]["scoped_to_topics"] == ["billing"]


async def test_patch_key_scope(pool, workspace_id):
    ids = await _seed_two_areas(pool, workspace_id)
    agent, key = await _scoped_client(pool, workspace_id, ["billing"])
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        patched = await admin.patch(
            f"/api/workspace/api-keys/{key['id']}",
            json={"allowed_topics": ["hiring"]})
        listed = (await admin.get("/api/workspace/api-keys")).json()
    assert patched.json()["allowed_topics"] == ["hiring"]
    assert any(k["id"] == key["id"] and k["allowed_topics"] == ["hiring"] for k in listed)
    async with agent:
        hidden = await agent.get(f"/api/agent/decisions/{ids['billing']}")
        ok = await agent.get(f"/api/agent/decisions/{ids['hiring']}")
    assert hidden.status_code == 404  # scope change applies without re-issuing
    assert ok.status_code == 200
