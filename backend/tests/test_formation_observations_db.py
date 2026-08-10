"""Database contracts for immutable, tenant-safe formation observations."""

import asyncpg
import pytest

from app.domains.documents.ingestion import IngestRequest, ingest_document


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
            f"formation-b-{doc_a}",
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
