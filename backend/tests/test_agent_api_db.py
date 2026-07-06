"""Agent API (/api/agent/*): structured ask with enriched citations,
deterministic warnings, search, decision evidence chains with supersession,
task context, workspace isolation, and usage attribution."""

import httpx

from app.core import usage
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import graph, worker
from app.providers import llm

from conftest import make_formation_result
from test_api_endpoints import _auth_client, _transport


async def _agent_client(pool, workspace_id) -> httpx.AsyncClient:
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        resp = await admin.post("/api/workspace/api-keys", json={"name": "test"})
    token = resp.json()["token"]
    return httpx.AsyncClient(
        transport=_transport(), base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _form_reversed_decision(pool, workspace_id, fake_llm):
    """Ingest + form one document whose extracted decision is REVERSED,
    leaving real chunks, chunk_links, and a reversed decision node behind."""
    fake_llm.result = make_formation_result(decisions=[{
        **make_formation_result()["decisions"][0],
        "title": "Use MySQL for the booking database",
        "status": "reversed",
    }])
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Booking DB choice",
                      text="We picked MySQL for the booking database. "
                           "Later we reversed this in favor of PostgreSQL."),
        workspace_id=workspace_id,
    )
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)
    async with pool.acquire() as conn:
        chunk = await conn.fetchrow(
            "SELECT id, text FROM chunks WHERE document_id=$1 ORDER BY chunk_index LIMIT 1",
            doc_id)
        node_id = await conn.fetchval(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND kind='decision'",
            workspace_id)
    return doc_id, dict(chunk), node_id


async def test_ask_returns_enriched_answer_and_warnings(pool, workspace_id, fake_llm, monkeypatch):
    doc_id, chunk, node_id = await _form_reversed_decision(pool, workspace_id, fake_llm)
    quote = "We picked MySQL for the booking database."

    async def _agent_answer(system, user, schema, **kw):
        # what the real anthropic branch does: record usage, return per schema
        await usage.record("llm", "anthropic", "claude-fable-5",
                           input_tokens=500, output_tokens=100)
        return {
            "answer": f"MySQL was chosen but later reversed [C{chunk['id']}].",
            "takeaway": "The MySQL decision was reversed.",
            "confidence": "high",
            "citations": [
                {"chunk_id": chunk["id"], "quote": quote},
                {"chunk_id": 999999, "quote": "hallucinated"},  # must be dropped
            ],
        }

    monkeypatch.setattr(llm, "structured_call", _agent_answer)
    async with await _agent_client(pool, workspace_id) as agent:
        resp = await agent.post("/api/agent/ask",
                                json={"question": "Which booking database do we use?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == "high"
    assert body["takeaway"]
    # hallucinated citation dropped; real one enriched and quote verified verbatim
    assert len(body["citations"]) == 1
    cit = body["citations"][0]
    assert cit["chunk_id"] == chunk["id"]
    assert cit["source"] == "meeting"
    assert cit["title"] == "Booking DB choice"
    assert cit["quote"] == quote
    assert cit["snippet"]
    # the reversed decision produced a deterministic warning
    assert any(w["type"] == "reversed_decision" and w["node_id"] == node_id
               for w in body["warnings"])
    assert any(n["id"] == node_id and n["status"] == "reversed"
               for n in body["nodes"])
    # usage attributed to the agent surface
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT surface, workspace_id FROM usage_events WHERE kind='llm' "
            "ORDER BY id DESC LIMIT 1")
    assert row["surface"] == "agent"
    assert row["workspace_id"] == workspace_id


async def test_ask_rejects_empty_question(pool, workspace_id):
    async with await _agent_client(pool, workspace_id) as agent:
        resp = await agent.post("/api/agent/ask", json={"question": "   "})
    assert resp.status_code == 400


async def test_context_is_deterministic_briefing(pool, workspace_id, fake_llm):
    _, _, node_id = await _form_reversed_decision(pool, workspace_id, fake_llm)
    async with await _agent_client(pool, workspace_id) as agent:
        resp = await agent.post(
            "/api/agent/context",
            json={"task": "Change the booking database schema",
                  "topics": ["database"]})
    assert resp.status_code == 200
    body = resp.json()
    assert any(d["node_id"] == node_id and d["status"] == "reversed"
               for d in body["relevant_decisions"])
    assert any(w["type"] == "reversed_decision" for w in body["warnings"])
    assert body["sources"] and body["sources"][0]["source"] == "meeting"
    assert "trace" in body


async def test_search_filters_and_scores(pool, workspace_id, fake_llm):
    await _form_reversed_decision(pool, workspace_id, fake_llm)
    async with await _agent_client(pool, workspace_id) as agent:
        hits = (await agent.get("/api/agent/search?q=MySQL")).json()
        decisions_only = (await agent.get(
            "/api/agent/search?q=MySQL&kind=decision&status=reversed")).json()
        misses = (await agent.get("/api/agent/search?q=zzz-nothing")).json()
    assert any(h["kind"] == "decision" for h in hits)
    assert decisions_only and all(
        h["kind"] == "decision" and h["status"] == "reversed" for h in decisions_only)
    assert all(0.0 <= h["confidence"] <= 1.0 for h in decisions_only)
    assert misses == []


async def test_decision_detail_with_supersession(pool, workspace_id):
    async with pool.acquire() as conn:
        old = await graph.upsert_node(
            conn, workspace_id, "decision", "Use MySQL everywhere",
            summary="Original choice.", status="reversed")
        new = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL everywhere",
            summary="Replacement choice.", status="decided",
            data={"made_by": ["Alice"]})
        await graph.add_edge(conn, workspace_id, new, old, "revisits")
    async with await _agent_client(pool, workspace_id) as agent:
        new_detail = (await agent.get(f"/api/agent/decisions/{new}")).json()
        old_detail = (await agent.get(f"/api/agent/decisions/{old}")).json()
        missing = await agent.get("/api/agent/decisions/999999")
    assert [r["node_id"] for r in new_detail["supersedes"]] == [old]
    assert new_detail["superseded_by"] == []
    assert [r["node_id"] for r in old_detail["superseded_by"]] == [new]
    assert 0.0 <= new_detail["confidence"] <= 1.0
    assert new_detail["made_by"] == ["Alice"]
    assert "evidence" in new_detail
    assert missing.status_code == 404


async def test_workspace_isolation(pool, workspace_id, fake_llm):
    """A key minted in workspace A must never see workspace B's memory."""
    async with pool.acquire() as conn:
        other_ws = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('other', 'other-iso') "
            "ON CONFLICT DO NOTHING RETURNING id")
        if other_ws is None:
            other_ws = await conn.fetchval(
                "SELECT id FROM workspaces WHERE slug='other-iso'")
        foreign_node = await graph.upsert_node(
            conn, other_ws, "decision", "Secret roadmap decision",
            summary="Belongs to the other workspace.", status="decided")
    async with await _agent_client(pool, workspace_id) as agent:
        detail = await agent.get(f"/api/agent/decisions/{foreign_node}")
        hits = (await agent.get("/api/agent/search?q=Secret roadmap")).json()
    assert detail.status_code == 404
    assert hits == []
