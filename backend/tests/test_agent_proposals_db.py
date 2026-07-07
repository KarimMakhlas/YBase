"""Agent write-back (/api/agent/propose + /api/memory-review/proposals):
proposals queue as pending rows without touching the memory graph; a curator
approves them into curated nodes with topic edges (merging on label like
formation) or rejects them; agents can only see their own workspace's queue.
"""

from app.domains.memory import graph

from test_agent_api_db import _agent_client
from test_api_endpoints import _auth_client

PROPOSAL = {
    "label": "Adopt pgvector for embeddings",
    "summary": "We evaluated Pinecone and pgvector; pgvector keeps everything in Postgres.",
    "status": "decided",
    "topics": ["Database", "embeddings", "database"],  # dup + case to normalize
    "data": {"made_by": ["Alice"]},
}


async def _propose(pool, workspace_id, **over):
    async with await _agent_client(pool, workspace_id) as agent:
        resp = await agent.post("/api/agent/propose", json={**PROPOSAL, **over})
    return resp


async def test_propose_queues_pending_without_touching_graph(pool, workspace_id):
    resp = await _propose(pool, workspace_id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["topics"] == ["database", "embeddings"]  # lowered, deduped
    assert body.get("warnings") is None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM memory_proposals WHERE id=$1", body["proposal_id"])
        nodes = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE workspace_id=$1", workspace_id)
        audit = await conn.fetchrow(
            "SELECT * FROM audit_events WHERE action='agent_proposal_created' "
            "AND workspace_id=$1", workspace_id)
    assert row["status"] == "pending"
    assert row["api_key_id"] is not None
    assert nodes == 0  # nothing live until a human approves
    assert audit is not None and audit["data"]["key_name"] == "test"


async def test_propose_validation(pool, workspace_id):
    assert (await _propose(pool, workspace_id, topics=[])).status_code == 400
    assert (await _propose(pool, workspace_id, label="  ")).status_code == 400
    assert (await _propose(pool, workspace_id, status="banana")).status_code == 400
    assert (await _propose(pool, workspace_id, kind="entity")).status_code == 400
    # question kind takes question statuses, not decision ones
    assert (await _propose(pool, workspace_id, kind="question",
                           status="open")).status_code == 200


async def test_propose_warns_when_active_node_exists(pool, workspace_id):
    async with pool.acquire() as conn:
        node_id = await graph.upsert_node(
            conn, workspace_id, "decision", PROPOSAL["label"],
            summary="Existing.", status="decided")
    resp = await _propose(pool, workspace_id)
    warnings = resp.json()["warnings"]
    assert warnings and warnings[0]["type"] == "existing_node"
    assert warnings[0]["node_id"] == node_id


async def test_approve_creates_curated_node_with_topic_edges(pool, workspace_id):
    proposal_id = (await _propose(pool, workspace_id)).json()["proposal_id"]

    admin, user_id = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        pending = (await admin.get("/api/memory-review/proposals")).json()
        assert [p["id"] for p in pending] == [proposal_id]
        assert pending[0]["key_name"] == "test"
        assert pending[0]["existing_node_id"] is None
        resp = await admin.post(
            f"/api/memory-review/proposals/{proposal_id}/approve",
            json={"summary": "Curator-tightened summary."})
    assert resp.status_code == 200
    node_id = resp.json()["node_id"]

    async with pool.acquire() as conn:
        node = await conn.fetchrow("SELECT * FROM memory_nodes WHERE id=$1", node_id)
        topics = await conn.fetch(
            "SELECT n.label FROM memory_edges e JOIN memory_nodes n ON n.id=e.dst "
            "WHERE e.src=$1 AND e.relation='about' AND n.kind='topic'", node_id)
        prop = await conn.fetchrow(
            "SELECT * FROM memory_proposals WHERE id=$1", proposal_id)
    assert node["kind"] == "decision"
    assert node["summary"] == "Curator-tightened summary."
    assert node["status"] == "decided"
    assert node["curated_at"] is not None and node["curated_by"] == user_id
    assert node["data"]["proposal_id"] == proposal_id
    assert sorted(t["label"] for t in topics) == ["database", "embeddings"]
    assert prop["status"] == "approved"
    assert prop["created_node_id"] == node_id
    assert prop["reviewed_by"] == user_id


async def test_approve_merges_into_existing_active_node(pool, workspace_id):
    async with pool.acquire() as conn:
        existing = await graph.upsert_node(
            conn, workspace_id, "decision", PROPOSAL["label"],
            summary="Short.", status="proposed")
    proposal_id = (await _propose(pool, workspace_id)).json()["proposal_id"]
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        resp = await admin.post(
            f"/api/memory-review/proposals/{proposal_id}/approve", json={})
    assert resp.json()["node_id"] == existing
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 "
            "AND kind='decision'", workspace_id)
        status = await conn.fetchval(
            "SELECT status FROM memory_nodes WHERE id=$1", existing)
    assert count == 1  # merged, not duplicated
    assert status == "decided"  # proposal's status won via upsert merge


async def test_reject_leaves_no_node_and_double_review_conflicts(pool, workspace_id):
    proposal_id = (await _propose(pool, workspace_id)).json()["proposal_id"]
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        resp = await admin.post(
            f"/api/memory-review/proposals/{proposal_id}/reject",
            json={"note": "Not an actual decision."})
        again = await admin.post(
            f"/api/memory-review/proposals/{proposal_id}/approve", json={})
    assert resp.status_code == 200
    assert again.status_code == 409  # already rejected — no resurrection
    async with pool.acquire() as conn:
        nodes = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE workspace_id=$1", workspace_id)
        prop = await conn.fetchrow(
            "SELECT status, resolution_note, created_node_id FROM memory_proposals "
            "WHERE id=$1", proposal_id)
    assert nodes == 0
    assert prop["status"] == "rejected"
    assert prop["resolution_note"] == "Not an actual decision."
    assert prop["created_node_id"] is None


async def test_member_cannot_review_proposals(pool, workspace_id):
    proposal_id = (await _propose(pool, workspace_id)).json()["proposal_id"]
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        listed = await member.get("/api/memory-review/proposals")
        approved = await member.post(
            f"/api/memory-review/proposals/{proposal_id}/approve", json={})
    assert listed.status_code == 403
    assert approved.status_code == 403


async def test_agent_sees_only_its_workspace_proposals(pool, workspace_id):
    proposal_id = (await _propose(pool, workspace_id)).json()["proposal_id"]
    async with pool.acquire() as conn:
        other_ws = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('other', 'other-prop') "
            "ON CONFLICT DO NOTHING RETURNING id")
        if other_ws is None:
            other_ws = await conn.fetchval(
                "SELECT id FROM workspaces WHERE slug='other-prop'")
    async with await _agent_client(pool, other_ws) as foreign:
        listed = (await foreign.get("/api/agent/proposals")).json()
        detail = await foreign.get(f"/api/agent/proposals/{proposal_id}")
    assert listed == []
    assert detail.status_code == 404

    # the proposing workspace's agent can track the outcome
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        await admin.post(f"/api/memory-review/proposals/{proposal_id}/approve", json={})
    async with await _agent_client(pool, workspace_id) as agent:
        mine = (await agent.get(f"/api/agent/proposals/{proposal_id}")).json()
    assert mine["status"] == "approved"
    assert mine["created_node_id"] is not None
