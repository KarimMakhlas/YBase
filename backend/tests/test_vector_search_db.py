"""Integration coverage for tenant-scoped pgvector retrieval."""

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.query import retrieval
from app.domains.query.vector_search import (
    approximate_vector_search,
    exact_vector_search,
    recall_at_k,
)


async def _seed_documents(workspace_id, prefix, count):
    for index in range(count):
        await ingest_document(
            IngestRequest(
                source="test",
                title=f"{prefix} document {index}",
                text=(
                    f"{prefix} retrieval corpus item {index}. "
                    f"It contains database ownership and billing retry detail {index}."
                ),
            ),
            workspace_id=workspace_id,
        )


async def test_vector_search_keeps_candidates_in_the_requested_workspace(
    pool, workspace_id
):
    async with pool.acquire() as conn:
        sibling_workspace_id = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES($1, $2) "
            "ON CONFLICT DO NOTHING RETURNING id",
            f"Vector sibling {workspace_id}", f"vector-sibling-{workspace_id}",
        )
        if sibling_workspace_id is None:
            sibling_workspace_id = await conn.fetchval(
                "SELECT id FROM workspaces WHERE lower(slug)=$1",
                f"vector-sibling-{workspace_id}",
            )

    await _seed_documents(workspace_id, f"workspace-{workspace_id}", 14)
    await _seed_documents(sibling_workspace_id, f"workspace-{sibling_workspace_id}", 14)

    async with pool.acquire() as conn:
        query = await conn.fetchrow(
            "SELECT id, embedding::text AS embedding, embed_model "
            "FROM chunks WHERE workspace_id=$1 ORDER BY id LIMIT 1",
            workspace_id,
        )
        approximate = await approximate_vector_search(
            conn,
            qvec=query["embedding"],
            workspace_id=workspace_id,
            embed_model=query["embed_model"],
            limit=10,
            candidate_multiplier=4,
            exclude_chunk_id=query["id"],
        )
        exact = await exact_vector_search(
            conn,
            qvec=query["embedding"],
            workspace_id=workspace_id,
            embed_model=query["embed_model"],
            limit=10,
            exclude_chunk_id=query["id"],
        )
        owners = await conn.fetch(
            "SELECT DISTINCT workspace_id FROM chunks WHERE id = ANY($1::int[])",
            [row["id"] for row in approximate.rows],
        )

    assert len(approximate.rows) == 10
    assert len(exact.rows) == 10
    assert {row["workspace_id"] for row in owners} == {workspace_id}
    assert query["id"] not in {row["id"] for row in approximate.rows}
    assert recall_at_k(
        [row["id"] for row in exact.rows],
        [row["id"] for row in approximate.rows],
        10,
    ) >= 0.95


async def test_hnsw_plan_remains_usable_with_workspace_and_model_filters(
    pool, workspace_id
):
    await _seed_documents(workspace_id, f"plan-{workspace_id}", 12)

    async with pool.acquire() as conn:
        query = await conn.fetchrow(
            "SELECT embedding::text AS embedding, embed_model "
            "FROM chunks WHERE workspace_id=$1 ORDER BY id LIMIT 1",
            workspace_id,
        )
        async with conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off")
            await conn.execute("SET LOCAL enable_sort = off")
            rows = await conn.fetch(
                "EXPLAIN (FORMAT TEXT) "
                "SELECT c.id FROM chunks c "
                "WHERE c.workspace_id=$1 AND c.embed_model=$2 "
                "ORDER BY c.embedding <=> $3::vector LIMIT 10",
                workspace_id,
                query["embed_model"],
                query["embedding"],
            )

    plan = "\n".join(row[0] for row in rows)
    assert "chunks_embedding_idx" in plan
    assert "workspace_id" in plan


async def test_retrieve_preserves_contract_and_reports_vector_diagnostics(
    workspace_id,
):
    await _seed_documents(workspace_id, f"retrieve-{workspace_id}", 12)

    result = await retrieval.retrieve(
        "billing retry database ownership",
        workspace_id=workspace_id,
    )

    assert set(result) == {"chunks", "nodes", "edges", "trace"}
    assert result["chunks"]
    assert "vector_candidates_scanned" in result["trace"]
    assert "hnsw_iterative_scan" in result["trace"]
