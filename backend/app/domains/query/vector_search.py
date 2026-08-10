"""Tenant-aware pgvector search helpers."""

import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

import asyncpg

from app.core import config


_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_iterative_scan_supported: Optional[bool] = None


@dataclass(frozen=True)
class VectorSearchResult:
    rows: List[Any]
    candidates_scanned: int
    iterative_scan_enabled: bool


def supports_iterative_hnsw(version: Optional[str]) -> bool:
    """Return whether the installed pgvector supports iterative HNSW scans."""
    if not version:
        return False
    match = _VERSION_RE.match(version)
    return bool(match and tuple(int(part) for part in match.groups()) >= (0, 8, 0))


def recall_at_k(
    exact_ids: Sequence[int], approximate_ids: Sequence[int], k: int
) -> float:
    """Measure approximate top-k overlap against the exact result set."""
    expected = set(exact_ids[:max(0, k)])
    if not expected:
        return 1.0
    actual = set(approximate_ids[:max(0, k)])
    return len(expected & actual) / len(expected)


async def _supports_iterative_scan(conn: asyncpg.Connection) -> bool:
    global _iterative_scan_supported
    if _iterative_scan_supported is None:
        version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        )
        _iterative_scan_supported = supports_iterative_hnsw(version)
    return _iterative_scan_supported


_VECTOR_CANDIDATE_SQL = """
WITH candidates AS MATERIALIZED (
    SELECT c.id, c.text, c.document_id, d.source, d.title, d.author,
           d.doc_created_at, ce.embedding <=> $1::vector AS distance
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE ce.workspace_id = $2
      AND c.workspace_id = $2
      AND d.workspace_id = $2
      AND d.is_active = true
      AND ce.embedding_model_id = $3
      AND ($6::int IS NULL OR c.id <> $6)
    ORDER BY ce.embedding <=> $1::vector
    LIMIT $4
)
SELECT id, text, document_id, source, title, author, doc_created_at,
       1 - distance AS score,
       count(*) OVER()::int AS candidate_count
FROM candidates
ORDER BY distance, id
LIMIT $5
"""


async def approximate_vector_search(
    conn: asyncpg.Connection,
    *,
    qvec: str,
    workspace_id: int,
    embedding_model_id: int,
    limit: int,
    candidate_multiplier: Optional[int] = None,
    exclude_chunk_id: Optional[int] = None,
) -> VectorSearchResult:
    """Return exact-ranked HNSW candidates scoped to one workspace/model."""
    final_limit = max(1, limit)
    multiplier = max(
        1,
        candidate_multiplier
        if candidate_multiplier is not None
        else config.VECTOR_CANDIDATE_MULTIPLIER,
    )
    candidate_limit = final_limit * multiplier
    iterative = False
    async with conn.transaction():
        if config.HNSW_ITERATIVE_SCAN and await _supports_iterative_scan(conn):
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            iterative = True
        rows = list(
            await conn.fetch(
                _VECTOR_CANDIDATE_SQL,
                qvec,
                workspace_id,
                embedding_model_id,
                candidate_limit,
                final_limit,
                exclude_chunk_id,
            )
        )
    scanned = rows[0]["candidate_count"] if rows else 0
    return VectorSearchResult(rows, scanned, iterative)


async def exact_vector_search(
    conn: asyncpg.Connection,
    *,
    qvec: str,
    workspace_id: int,
    embedding_model_id: int,
    limit: int,
    exclude_chunk_id: Optional[int] = None,
) -> VectorSearchResult:
    """Return an exact tenant-filtered vector top-k for recall evaluation."""
    final_limit = max(1, limit)
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_indexscan = off")
        await conn.execute("SET LOCAL enable_bitmapscan = off")
        rows = list(
            await conn.fetch(
                _VECTOR_CANDIDATE_SQL,
                qvec,
                workspace_id,
                embedding_model_id,
                final_limit,
                final_limit,
                exclude_chunk_id,
            )
        )
    scanned = rows[0]["candidate_count"] if rows else 0
    return VectorSearchResult(rows, scanned, False)
