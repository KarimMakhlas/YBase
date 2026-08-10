"""Database contracts for immutable, tenant-safe formation observations."""

import asyncpg
import pytest
from uuid import uuid4

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory.formation import run_formation

from conftest import make_formation_result


def _req(**over):
    base = {
        "source": "meeting",
        "title": "Formation observation test",
        "text": "We decided to use PostgreSQL because transactions matter.",
    }
    base.update(over)
    return IngestRequest(**base)


async def test_only_one_active_run_can_exist_for_a_document_revision(pool, workspace_id):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    async with pool.acquire() as conn:
        revision_id = await conn.fetchval(
            "SELECT revision_id FROM documents WHERE id=$1", doc_id
        )
        first = await conn.fetchval(
            "INSERT INTO formation_runs(workspace_id, document_id, revision_id, status, is_active) "
            "VALUES($1, $2, $3, 'success', true) RETURNING id",
            workspace_id, doc_id, revision_id,
        )
        second = await conn.fetchval(
            "INSERT INTO formation_runs(workspace_id, document_id, revision_id, status, is_active) "
            "VALUES($1, $2, $3, 'success', false) RETURNING id",
            workspace_id, doc_id, revision_id,
        )
        assert first != second
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "UPDATE formation_runs SET is_active=true WHERE id=$1", second
            )


async def test_observation_evidence_rejects_a_chunk_from_another_workspace(pool, workspace_id):
    doc_a, _ = await ingest_document(_req(title="Workspace A"), workspace_id=workspace_id)
    async with pool.acquire() as conn:
        workspace_b = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('Workspace B', $1) RETURNING id",
            f"formation-b-{uuid4().hex}",
        )
    doc_b, _ = await ingest_document(_req(title="Workspace B"), workspace_id=workspace_b)

    async with pool.acquire() as conn:
        revision_a = await conn.fetchval(
            "SELECT revision_id FROM documents WHERE id=$1", doc_a
        )
        run_id = await conn.fetchval(
            "INSERT INTO formation_runs(workspace_id, document_id, revision_id, status) "
            "VALUES($1, $2, $3, 'success') RETURNING id",
            workspace_id, doc_a, revision_a,
        )
        observation_id = await conn.fetchval(
            "INSERT INTO memory_observations(formation_run_id, workspace_id, document_id, "
            "revision_id, kind, ordinal, payload, status) "
            "VALUES($1, $2, $3, $4, 'decision', 0, '{}'::jsonb, 'valid') RETURNING id",
            run_id, workspace_id, doc_a, revision_a,
        )
        foreign_chunk = await conn.fetchval(
            "SELECT id FROM chunks WHERE document_id=$1 ORDER BY id LIMIT 1", doc_b
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO observation_evidence(workspace_id, observation_id, chunk_id) "
                "VALUES($1, $2, $3)",
                workspace_id, observation_id, foreign_chunk,
            )


async def test_valid_formation_stores_immutable_observation_and_evidence(
    pool, workspace_id, fake_llm
):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT revision_id, prompt_version, status, is_active FROM formation_runs "
            "WHERE document_id=$1 ORDER BY id DESC LIMIT 1",
            doc_id,
        )
        observation = await conn.fetchrow(
            "SELECT id, kind, payload, status, model_provider, model_name, prompt_version "
            "FROM memory_observations WHERE document_id=$1 ORDER BY id LIMIT 1",
            doc_id,
        )
        evidence = await conn.fetchval(
            "SELECT count(*) FROM observation_evidence WHERE observation_id=$1",
            observation["id"],
        )

    assert run["revision_id"] is not None
    assert run["prompt_version"]
    assert run["status"] == "candidate"
    assert run["is_active"] is True
    assert observation["kind"] == "decision"
    assert observation["status"] == "valid"
    assert observation["payload"]["title"] == make_formation_result()["decisions"][0]["title"]
    assert observation["model_provider"]
    assert observation["model_name"]
    assert observation["prompt_version"] == run["prompt_version"]
    assert evidence == 1


async def test_observation_projects_only_edges_it_asserted(
    pool, workspace_id, fake_llm
):
    """A newly active observation must not inherit unrelated legacy edges
    merely because they happen to leave the same identity node."""
    from app.domains.memory import graph

    async with pool.acquire() as conn:
        decision = await graph.upsert_node(
            conn, workspace_id, "decision", "Retained decision", status="decided"
        )
        stale_topic = await graph.upsert_node(conn, workspace_id, "topic", "stale")
        await graph.add_edge(conn, workspace_id, decision, stale_topic, "about")

    decision_result = {
        **make_formation_result()["decisions"][0],
        "title": "Retained decision",
        "topics": ["fresh"],
    }
    fake_llm.result = make_formation_result(decisions=[decision_result])
    doc_id, _ = await ingest_document(_req(title="Exact edge projection"), workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        edges = await conn.fetch(
            "SELECT t.label FROM observation_edge_projections ep "
            "JOIN memory_observations o ON o.id=ep.observation_id "
            "JOIN memory_nodes t ON t.id=ep.dst_node_id "
            "WHERE o.document_id=$1 AND ep.src_node_id=$2 AND ep.relation='about' "
            "ORDER BY t.label",
            doc_id, decision,
        )

    assert [row["label"] for row in edges] == ["fresh"]


async def test_decision_supporting_entity_is_not_rebuilt_as_a_decision(
    pool, workspace_id, fake_llm
):
    """A decision can evidence a person/topic relationship without becoming
    that supporting node's primary field projection."""
    doc_id, _ = await ingest_document(_req(title="Entity support"), workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        entity = await conn.fetchrow(
            "SELECT summary, status, data FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='entity' AND label='Alice Chen'",
            workspace_id,
        )

    assert entity["summary"] is None
    assert entity["status"] is None
    assert entity["data"] == {"entity_kind": "person"}


async def test_primary_automated_fields_have_active_observation_lineage(
    pool, workspace_id, fake_llm
):
    doc_id, _ = await ingest_document(_req(title="Field lineage"), workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        fields = await conn.fetch(
            "SELECT fp.field_name FROM memory_field_projections fp "
            "JOIN memory_observations o ON o.id=fp.observation_id "
            "JOIN formation_runs r ON r.id=o.formation_run_id "
            "JOIN memory_nodes n ON n.id=fp.node_id "
            "WHERE o.document_id=$1 AND o.status='valid' AND r.is_active "
            "AND n.kind='decision' ORDER BY fp.field_name",
            doc_id,
        )

    assert [row["field_name"] for row in fields] == ["data", "label", "status", "summary"]


async def test_invalid_evidence_is_quarantined_without_a_chunk_link(
    pool, workspace_id, fake_llm
):
    fake_llm.result = make_formation_result(decisions=[{
        **make_formation_result()["decisions"][0],
        "evidence_chunk_indexes": [999],
    }])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        observation = await conn.fetchrow(
            "SELECT id, status, quarantine_reason FROM memory_observations "
            "WHERE document_id=$1 ORDER BY id LIMIT 1",
            doc_id,
        )
        evidence = await conn.fetchval(
            "SELECT count(*) FROM observation_evidence WHERE observation_id=$1",
            observation["id"],
        )
        projected = await conn.fetchval(
            "SELECT count(*) FROM chunk_links cl JOIN chunks c ON c.id=cl.chunk_id "
            "WHERE c.document_id=$1",
            doc_id,
        )

    assert observation["status"] == "quarantined"
    assert observation["quarantine_reason"] == "invalid evidence chunk indexes: [999]"
    assert evidence == 0
    assert projected == 0


async def test_reformation_retires_prior_observations_and_stale_projection(
    pool, workspace_id, fake_llm
):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)
    fake_llm.result = make_formation_result(decisions=[], entities=[], questions=[])
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        active_runs = await conn.fetchval(
            "SELECT count(*) FROM formation_runs WHERE document_id=$1 AND is_active",
            doc_id,
        )
        retired = await conn.fetchval(
            "SELECT count(*) FROM memory_observations WHERE document_id=$1 AND status='retired'",
            doc_id,
        )
        links = await conn.fetchval(
            "SELECT count(*) FROM chunk_links cl JOIN chunks c ON c.id=cl.chunk_id "
            "WHERE c.document_id=$1",
            doc_id,
        )

    assert active_runs == 1
    assert retired == 1
    assert links == 0


async def test_failed_replacement_keeps_the_existing_run_active(
    pool, workspace_id, fake_llm, monkeypatch
):
    from app.domains.memory import projection

    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)
    async with pool.acquire() as conn:
        first_run = await conn.fetchval(
            "SELECT id FROM formation_runs WHERE document_id=$1 AND is_active", doc_id
        )

    async def fail_projection(_conn, _run_id):
        raise RuntimeError("projection failed")

    monkeypatch.setattr(projection, "_record_candidate_projection", fail_projection)
    with pytest.raises(RuntimeError, match="projection failed"):
        await run_formation(doc_id)

    async with pool.acquire() as conn:
        active_run = await conn.fetchval(
            "SELECT id FROM formation_runs WHERE document_id=$1 AND is_active", doc_id
        )
        active_observations = await conn.fetchval(
            "SELECT count(*) FROM memory_observations o JOIN formation_runs r "
            "ON r.id=o.formation_run_id WHERE o.document_id=$1 "
            "AND o.status='valid' AND r.is_active",
            doc_id,
        )

    assert active_run == first_run
    assert active_observations == 1


async def test_reformation_rebuilds_a_same_identity_node_from_active_observations(
    pool, workspace_id, fake_llm
):
    first = make_formation_result()["decisions"][0]
    first = {
        **first,
        "what": "The original interpretation included a very long explanation that is obsolete.",
        "reasoning": "The original rationale is deliberately long so the old upsert wins without projection rebuilding.",
    }
    fake_llm.result = make_formation_result(decisions=[first])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await run_formation(doc_id)

    replacement = {
        **first,
        "what": "The replacement interpretation is concise.",
        "reasoning": "The new rationale supersedes the original interpretation.",
    }
    fake_llm.result = make_formation_result(decisions=[replacement])
    await run_formation(doc_id)

    async with pool.acquire() as conn:
        summary = await conn.fetchval(
            "SELECT n.summary FROM memory_nodes n JOIN observation_projections op "
            "ON op.node_id=n.id JOIN memory_observations o ON o.id=op.observation_id "
            "JOIN formation_runs r ON r.id=o.formation_run_id "
            "WHERE o.document_id=$1 AND r.is_active AND o.status='valid'",
            doc_id,
        )

    assert summary == (
        "The replacement interpretation is concise.\n\n"
        "Reasoning: The new rationale supersedes the original interpretation."
    )
