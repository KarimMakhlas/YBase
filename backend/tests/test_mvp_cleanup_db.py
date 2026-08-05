"""Migration coverage for the reduced MVP schema."""

from app.core import migrate


REMOVED_PROVIDERS = (
    "jira",
    "linear",
    "confluence",
    "discord",
    "googledocs",
    "figma",
)


async def _document_and_chunk(conn, workspace_id: int, source: str) -> tuple[int, int]:
    document_id = await conn.fetchval(
        "INSERT INTO documents(workspace_id, source, title, raw_text) "
        "VALUES($1, $2, $3, 'evidence') RETURNING id",
        workspace_id,
        source,
        f"{source} document",
    )
    chunk_id = await conn.fetchval(
        "INSERT INTO chunks(document_id, chunk_index, text, embedding, embed_model) "
        "VALUES($1, 0, 'evidence', $2::vector, 'local-hash') RETURNING id",
        document_id,
        "[" + ",".join(["0"] * 512) + "]",
    )
    return document_id, chunk_id


async def test_cleanup_migration_purges_removed_sources_without_losing_shared_memory(
    pool, workspace_id
):
    """A shared node survives when it still has supported-source evidence."""
    async with pool.acquire() as conn:
        _, supported_chunk = await _document_and_chunk(conn, workspace_id, "slack")
        _, removed_chunk = await _document_and_chunk(conn, workspace_id, "jira")
        shared_node = await conn.fetchval(
            "INSERT INTO memory_nodes(workspace_id, kind, label) "
            "VALUES($1, 'decision', 'shared') RETURNING id",
            workspace_id,
        )
        removed_only_node = await conn.fetchval(
            "INSERT INTO memory_nodes(workspace_id, kind, label) "
            "VALUES($1, 'decision', 'removed only') RETURNING id",
            workspace_id,
        )
        await conn.executemany(
            "INSERT INTO chunk_links(chunk_id, node_id) VALUES($1, $2)",
            [
                (supported_chunk, shared_node),
                (removed_chunk, shared_node),
                (removed_chunk, removed_only_node),
            ],
        )
        await conn.execute(
            "INSERT INTO source_connections(workspace_id, provider, name, external_workspace_id) "
            "VALUES($1, 'jira', 'Jira', 'legacy-jira')",
            workspace_id,
        )
        await conn.execute(
            "DELETE FROM schema_migrations WHERE version='0012_mvp_cleanup'"
        )

    assert await migrate.run() == ["0012_mvp_cleanup"]

    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM documents WHERE source = ANY($1::text[])",
            list(REMOVED_PROVIDERS),
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM source_connections WHERE provider = ANY($1::text[])",
            list(REMOVED_PROVIDERS),
        ) == 0
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM memory_nodes WHERE id=$1)", shared_node
        ) is True
        assert await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM memory_nodes WHERE id=$1)", removed_only_node
        ) is False

    assert await migrate.run() == []
