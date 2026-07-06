"""Formation audit trail: consolidation merges, status flips on existing
nodes, permanent failures, and the /api/ops/audit endpoint."""

from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import consolidate, graph, worker
from app.domains.memory.formation import run_formation

from conftest import make_formation_result
from test_api_endpoints import _auth_client


def _req(**over):
    base = dict(source="meeting", title="Audit test", text="Decision content here.")
    base.update(over)
    return IngestRequest(**base)


async def test_consolidation_merge_is_audited(pool, workspace_id, monkeypatch):
    # Near-identical signatures; a permissive threshold keeps the local hash
    # embedder's exact similarity out of the assertion.
    monkeypatch.setattr(config, "MERGE_SIM_THRESHOLD", 0.5)
    async with pool.acquire() as conn:
        keep = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL as primary database",
            summary="Postgres for transactions and reliability.", status="decided")
        drop = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL as the primary database",
            summary="Postgres for transactions and reliability wins.", status="decided")
    merged = await consolidate.merge_similar_decisions(workspace_id, [keep, drop])
    assert merged, "expected the near-duplicates to merge"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT target_id, data FROM audit_events "
            "WHERE workspace_id=$1 AND action='consolidation_merge_nodes'",
            workspace_id)
    assert row is not None
    assert int(row["target_id"]) == merged[0]["kept"]
    assert row["data"]["dropped"] == merged[0]["dropped"]
    assert row["data"]["sim"] >= 0.5
    assert row["data"]["kept_label"] and row["data"]["dropped_label"]


async def test_reversal_status_flip_is_audited(pool, workspace_id, fake_llm):
    async with pool.acquire() as conn:
        old_node = await graph.upsert_node(
            conn, workspace_id, "decision", "Use MySQL for the main database",
            summary="MySQL chosen early on.", status="decided")
    fake_llm.result = make_formation_result(decisions=[{
        **make_formation_result()["decisions"][0],
        "title": "Use PostgreSQL instead of MySQL",
        "status": "reversed",
        "revisits_node_id": old_node,
    }])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM memory_nodes WHERE id=$1", old_node)
        row = await conn.fetchrow(
            "SELECT target_id, data FROM audit_events "
            "WHERE workspace_id=$1 AND action='formation_node_status_change'",
            workspace_id)
    assert status == "reversed"
    assert row is not None
    assert int(row["target_id"]) == old_node
    assert row["data"]["old_status"] == "decided"
    assert row["data"]["new_status"] == "reversed"
    assert row["data"]["document_id"] == doc_id


async def test_question_resolution_flip_is_audited(pool, workspace_id, fake_llm):
    async with pool.acquire() as conn:
        q_node = await graph.upsert_node(
            conn, workspace_id, "question", "Which database should we use?",
            status="open")
    fake_llm.result = make_formation_result(decisions=[{
        **make_formation_result()["decisions"][0],
        "resolves_question_node_id": q_node,
    }])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM memory_nodes WHERE id=$1", q_node)
        row = await conn.fetchrow(
            "SELECT data FROM audit_events "
            "WHERE workspace_id=$1 AND action='formation_node_status_change' "
            "AND target_id=$2::text",
            workspace_id, str(q_node))
    assert status == "resolved"
    assert row is not None
    assert row["data"]["old_status"] == "open"
    assert row["data"]["new_status"] == "resolved"


async def test_permanent_failure_is_audited(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    for attempt in range(config.FORMATION_MAX_ATTEMPTS):
        await worker._record_failure(doc_id, f"boom {attempt}")

    async with pool.acquire() as conn:
        doc_status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id)
        row = await conn.fetchrow(
            "SELECT target_id, data FROM audit_events "
            "WHERE workspace_id=$1 AND action='formation_failed_permanently'",
            workspace_id)
    assert doc_status == "failed"
    assert row is not None
    assert int(row["target_id"]) == doc_id
    assert row["data"]["attempts"] == config.FORMATION_MAX_ATTEMPTS
    assert "boom" in row["data"]["error"]


async def test_audit_endpoint_lists_and_filters(pool, workspace_id):
    from app.domains.auth import service as auth_svc

    async with pool.acquire() as conn:
        await auth_svc.audit(conn, "consolidation_merge_nodes", workspace_id, None,
                             target_type="memory_node", target_id=1,
                             data={"sim": 0.9})
        await auth_svc.audit(conn, "formation_failed_permanently", workspace_id, None,
                             target_type="document", target_id=2,
                             data={"attempts": 3})
    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        everything = await client.get("/api/ops/audit?days=7")
        filtered = await client.get(
            "/api/ops/audit?days=7&actions=consolidation_merge_nodes")
        member_denied_client, _ = await _auth_client(pool, workspace_id, role="member")
    assert everything.status_code == 200
    actions = {e["action"] for e in everything.json()["events"]}
    assert {"consolidation_merge_nodes", "formation_failed_permanently"} <= actions
    assert filtered.status_code == 200
    assert {e["action"] for e in filtered.json()["events"]} == {"consolidation_merge_nodes"}
    async with member_denied_client:
        denied = await member_denied_client.get("/api/ops/audit")
    assert denied.status_code == 403
