"""Workspace-scoped embedding model registry and atomic activation helpers."""

from dataclasses import dataclass
from typing import Optional

import asyncpg


@dataclass(frozen=True)
class EmbeddingCoverage:
    active_chunks: int
    embedded_chunks: int

    @property
    def complete(self) -> bool:
        return self.active_chunks == self.embedded_chunks


@dataclass(frozen=True)
class NodeEmbeddingCoverage:
    active_nodes: int
    embedded_nodes: int

    @property
    def complete(self) -> bool:
        return self.active_nodes == self.embedded_nodes


async def ensure_model(conn: asyncpg.Connection, model_key: str) -> int:
    return await conn.fetchval(
        "INSERT INTO embedding_models(model_key) VALUES($1) "
        "ON CONFLICT (model_key) DO UPDATE SET model_key=EXCLUDED.model_key "
        "RETURNING id",
        model_key,
    )


async def active_model(conn: asyncpg.Connection, workspace_id: int) -> Optional[int]:
    return await conn.fetchval(
        "SELECT active_embedding_model_id FROM workspaces WHERE id=$1", workspace_id
    )


async def model_key(conn: asyncpg.Connection, embedding_model_id: int) -> Optional[str]:
    return await conn.fetchval(
        "SELECT model_key FROM embedding_models WHERE id=$1", embedding_model_id
    )


async def coverage(
    conn: asyncpg.Connection, workspace_id: int, embedding_model_id: int
) -> EmbeddingCoverage:
    row = await conn.fetchrow(
        "SELECT count(c.id)::int AS active_chunks, "
        "count(ce.chunk_id)::int AS embedded_chunks "
        "FROM chunks c JOIN documents d ON d.id=c.document_id "
        "LEFT JOIN chunk_embeddings ce ON ce.chunk_id=c.id "
        "AND ce.workspace_id=c.workspace_id AND ce.embedding_model_id=$2 "
        "WHERE c.workspace_id=$1 AND d.workspace_id=$1 AND d.is_active",
        workspace_id, embedding_model_id,
    )
    return EmbeddingCoverage(row["active_chunks"], row["embedded_chunks"])


async def node_coverage(
    conn: asyncpg.Connection, workspace_id: int, embedding_model_id: int
) -> NodeEmbeddingCoverage:
    row = await conn.fetchrow(
        "SELECT count(n.id)::int AS active_nodes, "
        "count(ne.node_id)::int AS embedded_nodes "
        "FROM memory_nodes n LEFT JOIN memory_node_embeddings ne "
        "ON ne.node_id=n.id AND ne.workspace_id=n.workspace_id "
        "AND ne.embedding_model_id=$2 "
        "WHERE n.workspace_id=$1 AND n.kind='decision' AND n.archived_at IS NULL",
        workspace_id, embedding_model_id,
    )
    return NodeEmbeddingCoverage(row["active_nodes"], row["embedded_nodes"])


async def activate_model(
    conn: asyncpg.Connection, workspace_id: int, embedding_model_id: int
) -> None:
    exists = await conn.fetchval(
        "SELECT 1 FROM embedding_models WHERE id=$1", embedding_model_id
    )
    if not exists:
        raise ValueError(f"embedding model {embedding_model_id} does not exist")
    current = await coverage(conn, workspace_id, embedding_model_id)
    if not current.complete:
        raise ValueError(
            f"embedding model {embedding_model_id} covers {current.embedded_chunks}/"
            f"{current.active_chunks} active chunks"
        )
    nodes = await node_coverage(conn, workspace_id, embedding_model_id)
    if not nodes.complete:
        raise ValueError(
            f"embedding model {embedding_model_id} covers {nodes.embedded_nodes}/"
            f"{nodes.active_nodes} active decision nodes"
        )
    await conn.execute(
        "UPDATE workspaces SET active_embedding_model_id=$2 WHERE id=$1",
        workspace_id, embedding_model_id,
    )
