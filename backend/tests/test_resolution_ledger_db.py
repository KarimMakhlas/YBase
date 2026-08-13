"""Reversible identity-resolution ledger contracts."""

from app.domains.memory import graph, resolver
from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory.formation import run_formation

from conftest import make_formation_result


async def test_resolution_records_a_reversible_merge(pool, workspace_id):
    async with pool.acquire() as conn:
        keep = await graph.upsert_node(conn, workspace_id, "decision", "Use PostgreSQL")
        drop = await graph.upsert_node(conn, workspace_id, "decision", "Use Postgres")
        ledger_id = await resolver.record_merge_candidate(conn, workspace_id, keep, drop, 0.99)
        row = await conn.fetchrow("SELECT survivor_node_id, retired_node_id, status FROM resolution_ledger WHERE id=$1", ledger_id)
    assert (row["survivor_node_id"], row["retired_node_id"], row["status"]) == (keep, drop, "candidate")


async def test_resolution_candidate_has_auditable_approval_and_revert_lifecycle(
    pool, workspace_id
):
    async with pool.acquire() as conn:
        keep = await graph.upsert_node(conn, workspace_id, "decision", "Use PostgreSQL")
        drop = await graph.upsert_node(conn, workspace_id, "decision", "Use Postgres")
        ledger_id = await resolver.record_merge_candidate(conn, workspace_id, keep, drop, 0.99)

        assert await resolver.set_candidate_status(
            conn, ledger_id, workspace_id, "approved"
        ) is True
        approved = await conn.fetchrow(
            "SELECT status, resolved_at FROM resolution_ledger WHERE id=$1", ledger_id
        )
        audit = await conn.fetchrow(
            "SELECT action, data FROM audit_events WHERE workspace_id=$1 "
            "AND target_id=$2::text ORDER BY id DESC LIMIT 1",
            workspace_id, str(ledger_id),
        )
        assert approved["status"] == "approved"
        assert approved["resolved_at"] is not None
        assert audit["action"] == "resolution_candidate_approved"
        assert audit["data"]["previous_status"] == "candidate"

        assert await resolver.set_candidate_status(
            conn, ledger_id, workspace_id, "reverted"
        ) is True
        reverted = await conn.fetchval(
            "SELECT status FROM resolution_ledger WHERE id=$1", ledger_id
        )
        node_count = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE id = ANY($1::int[])", [keep, drop]
        )

    assert reverted == "reverted"
    assert node_count == 2


async def test_automatic_resolution_requires_active_evidence_for_both_nodes(
    pool, workspace_id
):
    async with pool.acquire() as conn:
        keep = await graph.upsert_node(conn, workspace_id, "decision", "Use PostgreSQL")
        drop = await graph.upsert_node(conn, workspace_id, "decision", "Use Postgres")
        eligible = await resolver.eligible_for_automatic_resolution(
            conn, workspace_id, keep, drop, 0.999
        )
        node_count = await conn.fetchval(
            "SELECT count(*) FROM memory_nodes WHERE id = ANY($1::int[])", [keep, drop]
        )

    assert eligible is False
    assert node_count == 2


async def test_automatic_resolution_approves_only_high_confidence_evidenced_nodes(
    pool, workspace_id, fake_llm, monkeypatch
):
    monkeypatch.setattr(config, "RESOLVER_AUTO_THRESHOLD", 0.5)
    base = make_formation_result()["decisions"][0]
    fake_llm.result = make_formation_result(decisions=[
        {**base, "title": "Use PostgreSQL as primary database"},
        {**base, "title": "Use PostgreSQL as the primary database"},
    ])
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Duplicates", text="Choose PostgreSQL."),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        node_ids = [r["id"] for r in await conn.fetch(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND kind='decision' ORDER BY id",
            workspace_id,
        )]
        eligible = await resolver.eligible_for_automatic_resolution(
            conn, workspace_id, node_ids[0], node_ids[1], 0.99
        )
        ledger_id = await resolver.record_merge_candidate(
            conn, workspace_id, node_ids[0], node_ids[1], 0.99
        )
        status = await conn.fetchval(
            "SELECT status FROM resolution_ledger WHERE id=$1", ledger_id
        )
        audit = await conn.fetchrow(
            "SELECT action, data FROM audit_events WHERE workspace_id=$1 "
            "AND target_id=$2::text ORDER BY id DESC LIMIT 1",
            workspace_id, str(ledger_id),
        )

    assert eligible is True
    assert status == "approved"
    assert audit["action"] == "resolution_candidate_auto_approved"
    assert audit["data"]["similarity"] == 0.99
