"""Chronological memory-event contracts."""

from app.domains.memory import events, graph
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory.formation import run_formation
from conftest import make_formation_result


async def test_later_effective_event_wins_even_when_inserted_first(pool, workspace_id):
    async with pool.acquire() as conn:
        node_id = await graph.upsert_node(
            conn, workspace_id, "decision", "Use PostgreSQL", status="proposed"
        )
        await events.record_decision_event(
            conn, workspace_id, node_id, "reversed", "2026-06-01"
        )
        await events.record_decision_event(
            conn, workspace_id, node_id, "decided", "2026-01-15"
        )
        status = await events.derive_node_status(conn, node_id)
        projected = await conn.fetchval("SELECT status FROM memory_nodes WHERE id=$1", node_id)

    assert status == "reversed"
    assert projected == "reversed"


async def test_formation_records_a_dated_decision_event(pool, workspace_id, fake_llm):
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Decision", text="Use PostgreSQL."),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT event_type, effective_at, observation_id FROM memory_events"
        )
        observation_id = await conn.fetchval(
            "SELECT id FROM memory_observations WHERE document_id=$1 AND kind='decision'",
            doc_id,
        )
    assert event["event_type"] == make_formation_result()["decisions"][0]["status"]
    assert event["effective_at"].date().isoformat() == "2026-01-15"
    assert event["observation_id"] == observation_id


async def test_formation_records_an_observation_backed_question_event(
    pool, workspace_id, fake_llm
):
    fake_llm.result = make_formation_result(
        decisions=[],
        questions=[{
            "question": "Who owns the migration?",
            "status": "open",
            "resolution": None,
            "raised_by": ["Alice Chen"],
            "topics": ["database"],
            "evidence_chunk_indexes": [0],
            "resolves_node_id": None,
            "relates_to_node_ids": [],
        }],
    )
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Question", text="Who owns the migration?"),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT e.event_type, e.observation_id FROM memory_events e "
            "JOIN memory_nodes n ON n.id=e.node_id WHERE n.kind='question'"
        )
        observation_id = await conn.fetchval(
            "SELECT id FROM memory_observations WHERE document_id=$1 AND kind='question'",
            doc_id,
        )

    assert event["event_type"] == "open"
    assert event["observation_id"] == observation_id


async def test_reversal_of_an_existing_decision_is_an_observation_backed_event(
    pool, workspace_id, fake_llm
):
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Initial", text="Use PostgreSQL."),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        original_id = await conn.fetchval(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND kind='decision'",
            workspace_id,
        )

    reversal = {
        **make_formation_result()["decisions"][0],
        "title": "Replace PostgreSQL",
        "what": "Reversed the PostgreSQL decision.",
        "status": "reversed",
        "date": "2026-02-01",
        "revisits_node_id": original_id,
    }
    fake_llm.result = make_formation_result(decisions=[reversal])
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT event_type, observation_id FROM memory_events "
            "WHERE node_id=$1 AND event_type='reversed'",
            original_id,
        )
        observation_id = await conn.fetchval(
            "SELECT id FROM memory_observations WHERE document_id=$1 "
            "AND kind='decision' ORDER BY id DESC LIMIT 1",
            doc_id,
        )

    assert event["event_type"] == "reversed"
    assert event["observation_id"] == observation_id


async def test_retired_observation_event_cannot_override_active_run_state(
    pool, workspace_id, fake_llm
):
    first = {
        **make_formation_result()["decisions"][0],
        "status": "reversed",
        "date": "2026-06-01",
    }
    fake_llm.result = make_formation_result(decisions=[first])
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="State", text="Use PostgreSQL."),
        workspace_id=workspace_id,
    )
    await run_formation(doc_id)

    replacement = {**first, "status": "decided", "date": "2026-01-15"}
    fake_llm.result = make_formation_result(decisions=[replacement])
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM memory_nodes WHERE workspace_id=$1 AND kind='decision'",
            workspace_id,
        )
        active_event = await conn.fetchval(
            "SELECT e.event_type FROM memory_events e "
            "JOIN memory_observations o ON o.id=e.observation_id "
            "JOIN formation_runs r ON r.id=o.formation_run_id "
            "WHERE r.is_active ORDER BY e.effective_at DESC LIMIT 1"
        )

    assert active_event == "decided"
    assert status == "decided"
