"""Versioned embedding registry and activation contracts."""

import asyncpg
import pytest

from app.domains.query import embedding_versions
from app.domains.query.vector_search import approximate_vector_search
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.providers.embeddings import to_pgvector
from app.providers.embeddings import active_embed_model


async def test_workspace_resolves_one_active_embedding_model(pool, workspace_id):
    async with pool.acquire() as conn:
        first = await embedding_versions.ensure_model(conn, "test:first:512")
        second = await embedding_versions.ensure_model(conn, "test:second:512")
        await embedding_versions.activate_model(conn, workspace_id, first)
        assert await embedding_versions.active_model(conn, workspace_id) == first
        await embedding_versions.activate_model(conn, workspace_id, second)
        assert await embedding_versions.active_model(conn, workspace_id) == second


async def test_chunk_embedding_rejects_cross_workspace_provenance(pool, workspace_id):
    async with pool.acquire() as conn:
        workspace_b = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('Embedding B', 'embedding-b') "
            "ON CONFLICT DO NOTHING RETURNING id"
        ) or await conn.fetchval("SELECT id FROM workspaces WHERE lower(slug)='embedding-b'")
        document_id = await conn.fetchval(
            "INSERT INTO documents(workspace_id, source, title, raw_text) "
            "VALUES($1, 'test', 'Cross workspace', 'text') RETURNING id",
            workspace_id,
        )
        chunk_id = await conn.fetchval(
            "INSERT INTO chunks(workspace_id, document_id, chunk_index, text, embedding, embed_model, "
            "section_path, source_start, source_end, content_type, token_count) "
            "VALUES($1,$2,0,'text',$3::vector,'legacy:test',ARRAY['test'],0,4,'text/plain',1) RETURNING id",
            workspace_id, document_id, to_pgvector([0.0] * 512),
        )
        model_id = await embedding_versions.ensure_model(conn, "test:cross:512")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding) "
                "VALUES($1,$2,$3,$4::vector)",
                workspace_b, chunk_id, model_id, to_pgvector([0.0] * 512),
            )


async def test_ingest_writes_the_workspace_active_embedding_version(pool, workspace_id):
    async with pool.acquire() as conn:
        model_id = await embedding_versions.ensure_model(conn, await active_embed_model())
        await embedding_versions.activate_model(conn, workspace_id, model_id)

    document_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Versioned", text="Embedding version evidence."),
        workspace_id=workspace_id,
    )
    async with pool.acquire() as conn:
        chunk_id = await conn.fetchval(
            "SELECT id FROM chunks WHERE document_id=$1", document_id
        )
        stored_model = await conn.fetchval(
            "SELECT embedding_model_id FROM chunk_embeddings WHERE chunk_id=$1", chunk_id
        )

    assert stored_model == model_id


async def test_vector_search_accepts_the_workspace_active_model_identifier(pool, workspace_id):
    document_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Search version", text="Versioned search evidence."),
        workspace_id=workspace_id,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ce.embedding::text AS embedding, ce.embedding_model_id "
            "FROM chunk_embeddings ce JOIN chunks c ON c.id=ce.chunk_id "
            "WHERE c.document_id=$1",
            document_id,
        )
        result = await approximate_vector_search(
            conn,
            qvec=row["embedding"],
            workspace_id=workspace_id,
            embedding_model_id=row["embedding_model_id"],
            limit=1,
        )

    assert result.rows and result.rows[0]["id"]


async def test_activation_requires_complete_coverage_and_rollback_is_a_pointer_flip(
    pool, workspace_id
):
    document_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Coverage", text="Coverage requirement evidence."),
        workspace_id=workspace_id,
    )
    async with pool.acquire() as conn:
        current_model = await embedding_versions.active_model(conn, workspace_id)
        candidate = await embedding_versions.ensure_model(conn, "test:candidate:512")
        with pytest.raises(ValueError, match="covers 0/1"):
            await embedding_versions.activate_model(conn, workspace_id, candidate)

        await conn.execute(
            "INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding) "
            "SELECT workspace_id, chunk_id, $2, embedding FROM chunk_embeddings "
            "WHERE chunk_id=(SELECT id FROM chunks WHERE document_id=$1)",
            document_id, candidate,
        )
        await embedding_versions.activate_model(conn, workspace_id, candidate)
        assert await embedding_versions.active_model(conn, workspace_id) == candidate

        await embedding_versions.activate_model(conn, workspace_id, current_model)
        assert await embedding_versions.active_model(conn, workspace_id) == current_model


async def test_activation_requires_active_decision_embedding_coverage(pool, workspace_id):
    async with pool.acquire() as conn:
        current = await embedding_versions.ensure_model(conn, "test:node-current:512")
        candidate = await embedding_versions.ensure_model(conn, "test:node-candidate:512")
        await embedding_versions.activate_model(conn, workspace_id, current)
        node_id = await conn.fetchval(
            "INSERT INTO memory_nodes(workspace_id, kind, label, status) "
            "VALUES($1, 'decision', 'Versioned node coverage', 'decided') RETURNING id",
            workspace_id,
        )
        await conn.execute(
            "INSERT INTO memory_node_embeddings(workspace_id, node_id, embedding_model_id, embedding) "
            "VALUES($1,$2,$3,$4::vector)",
            workspace_id, node_id, current, to_pgvector([0.0] * 512),
        )

        with pytest.raises(ValueError, match="covers 0/1 active decision nodes"):
            await embedding_versions.activate_model(conn, workspace_id, candidate)


async def test_initial_chunk_model_activation_can_bootstrap_before_node_backfill(pool, workspace_id):
    async with pool.acquire() as conn:
        model = await embedding_versions.ensure_model(conn, "test:bootstrap:512")
        await conn.execute(
            "INSERT INTO memory_nodes(workspace_id, kind, label, status) "
            "VALUES($1, 'decision', 'Manual decision awaiting consolidation', 'decided')",
            workspace_id,
        )

        await embedding_versions.activate_model(
            conn, workspace_id, model, require_node_coverage=False
        )

    async with pool.acquire() as conn:
        assert await embedding_versions.active_model(conn, workspace_id) == model
