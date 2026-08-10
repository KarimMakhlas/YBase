"""Ingestion pipeline: dedup → chunk → embed → store → enqueue formation."""

import hashlib
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from app.core import config, db, usage
from app.providers.embeddings import (
    EmbeddingSpaceMismatch,
    active_embed_model,
    embed_texts,
    to_pgvector,
)
from ..query import embedding_versions
from ..memory import worker


async def schedule_formation(doc_id: int) -> None:
    await worker.enqueue(doc_id)


class IngestRequest(BaseModel):
    source: str = Field(..., description="slack | notion | github | jira | meeting | other",
                        max_length=64)
    title: str = Field(..., max_length=1000)
    # Bounded so one request can't buffer an arbitrarily large document through
    # chunking and embedding. Connectors chunk long sources upstream anyway.
    text: str = Field(..., max_length=config.MAX_DOCUMENT_CHARS)
    author: Optional[str] = Field(None, max_length=500)
    created_at: Optional[str] = None  # ISO 8601 — when the content was originally written
    tags: List[str] = Field(default_factory=list)
    source_connection_id: Optional[int] = None
    source_stream_id: Optional[int] = None
    external_ref: Optional[str] = None
    idempotency_key: Optional[str] = Field(None, max_length=256)


def content_hash(source: str, title: str, text: str) -> str:
    return hashlib.sha256(f"{source}\n{title}\n{text}".encode()).hexdigest()


def source_identity(req: IngestRequest, digest: str) -> str:
    """Return the stable identity used to serialize one source object's revisions."""
    if req.source_connection_id is not None and req.external_ref:
        return f"connector:{req.source_connection_id}:{req.external_ref}"
    if req.idempotency_key:
        return f"upload:{req.idempotency_key}"
    return f"content:{digest}"


def chunk_text(text: str, target: int = 900, hard_max: int = 1500) -> List[str]:
    """Paragraph-aware chunking: greedily pack paragraphs up to ~target chars,
    hard-splitting any single paragraph longer than hard_max."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    cur = ""
    for p in paras:
        while len(p) > hard_max:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:hard_max])
            p = p[hard_max:].strip()
        if not p:
            continue
        if cur and len(cur) + len(p) + 2 > target:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks or [text[:hard_max]]


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # date-only inputs must not shift a day when stored as timestamptz
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def accept_revision(
    req: IngestRequest, workspace_id: int
) -> Tuple[int, int, bool]:
    """Persist an immutable source revision before any embedding provider call.

    Returns ``(document_id, revision_id, duplicate)``. The source-object row is
    locked by its unique identity inside the transaction, so concurrent callers
    for one connector object or upload key serialize their revision numbering.
    """
    pool = await db.get_pool()
    digest = content_hash(req.source, req.title, req.text)
    identity = source_identity(req, digest)
    async with pool.acquire() as conn:
        async with conn.transaction():
            source_object = await conn.fetchrow(
                "INSERT INTO source_objects("
                "workspace_id, identity_key, source_connection_id, source_stream_id, external_ref"
                ") VALUES($1, $2, $3, $4, $5) "
                "ON CONFLICT (workspace_id, identity_key) DO UPDATE SET "
                "source_connection_id=COALESCE(EXCLUDED.source_connection_id, source_objects.source_connection_id), "
                "source_stream_id=COALESCE(EXCLUDED.source_stream_id, source_objects.source_stream_id), "
                "external_ref=COALESCE(EXCLUDED.external_ref, source_objects.external_ref), "
                "status='active', deleted_at=NULL, updated_at=now() "
                "RETURNING id",
                workspace_id,
                identity,
                req.source_connection_id,
                req.source_stream_id,
                req.external_ref,
            )
            source_object_id = source_object["id"]
            existing = await conn.fetchrow(
                "SELECT d.id AS document_id, r.id AS revision_id "
                "FROM document_revisions r "
                "JOIN documents d ON d.revision_id=r.id "
                "WHERE r.source_object_id=$1 AND r.content_hash=$2",
                source_object_id,
                digest,
            )
            if existing is not None:
                return existing["document_id"], existing["revision_id"], True

            revision_number = await conn.fetchval(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 "
                "FROM document_revisions WHERE source_object_id=$1",
                source_object_id,
            )
            revision_id = await conn.fetchval(
                "INSERT INTO document_revisions("
                "workspace_id, source_object_id, revision_number, content_hash, source, "
                "title, author, doc_created_at, raw_text, tags"
                ") VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
                workspace_id,
                source_object_id,
                revision_number,
                digest,
                req.source,
                req.title,
                req.author,
                _parse_date(req.created_at),
                req.text,
                req.tags,
            )
            await conn.execute(
                "UPDATE documents SET is_active=false "
                "WHERE source_object_id=$1 AND is_active",
                source_object_id,
            )
            doc_id = await conn.fetchval(
                "INSERT INTO documents("
                "workspace_id, source, title, author, doc_created_at, raw_text, tags, content_hash, "
                "source_connection_id, source_stream_id, external_ref, source_object_id, revision_id"
                ") VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING id",
                workspace_id,
                req.source,
                req.title,
                req.author,
                _parse_date(req.created_at),
                req.text,
                req.tags,
                digest,
                req.source_connection_id,
                req.source_stream_id,
                req.external_ref,
                source_object_id,
                revision_id,
            )
            await conn.execute(
                "UPDATE source_objects SET current_revision_id=$2, status='active', "
                "deleted_at=NULL, updated_at=now() WHERE id=$1",
                source_object_id,
                revision_id,
            )
    return doc_id, revision_id, False


async def _mark_materialization_failed(
    document_id: int, revision_id: int, error: Exception
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE document_revisions SET status='failed', error=$2 WHERE id=$1",
            revision_id,
            str(error)[:2000],
        )
        await conn.execute(
            "UPDATE documents SET formation_status='failed', formation_error=$2 WHERE id=$1",
            document_id,
            str(error)[:2000],
        )


async def _materialize_revision(
    req: IngestRequest, workspace_id: int, document_id: int, revision_id: int
) -> None:
    """Embed and store chunks for an already durable accepted revision."""
    pool = await db.get_pool()
    try:
        embed_model = await active_embed_model()
        async with pool.acquire() as conn:
            model_id = await embedding_versions.ensure_model(conn, embed_model)
            workspace_model_id = await embedding_versions.active_model(conn, workspace_id)
            if workspace_model_id is None:
                await embedding_versions.activate_model(conn, workspace_id, model_id)
            elif workspace_model_id != model_id:
                existing_models = await conn.fetch(
                    "SELECT model_key FROM embedding_models WHERE id=$1", workspace_model_id
                )
                raise EmbeddingSpaceMismatch(
                    embed_model, [row["model_key"] for row in existing_models]
                )
            await conn.execute(
                "UPDATE document_revisions SET status='materializing', error=NULL WHERE id=$1",
                revision_id,
            )

        pieces = chunk_text(req.text)
        usage_token = usage.set_context(workspace_id=workspace_id, surface="ingest")
        try:
            embeddings = await embed_texts(pieces)
        finally:
            usage.reset_context(usage_token)

        async with pool.acquire() as conn:
            async with conn.transaction():
                for i, (piece, emb) in enumerate(zip(pieces, embeddings)):
                    chunk_id = await conn.fetchval(
                        "INSERT INTO chunks(workspace_id, document_id, chunk_index, text, embedding, embed_model) "
                        "VALUES($1, $2, $3, $4, $5::vector, $6) RETURNING id",
                        workspace_id, document_id, i, piece, to_pgvector(emb), embed_model,
                    )
                    await conn.execute(
                        "INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding) "
                        "VALUES($1, $2, $3, $4::vector)",
                        workspace_id, chunk_id, model_id, to_pgvector(emb),
                    )
                await conn.execute(
                    "UPDATE document_revisions SET status='searchable', error=NULL WHERE id=$1",
                    revision_id,
                )
    except Exception as exc:
        await _mark_materialization_failed(document_id, revision_id, exc)
        raise


async def mark_source_deleted_for_document(document_id: int, workspace_id: int) -> bool:
    """Record a provider-reported deletion without erasing revision history."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT d.source_object_id, d.revision_id "
                "FROM documents d JOIN source_objects so ON so.id=d.source_object_id "
                "WHERE d.id=$1 AND d.workspace_id=$2 AND d.is_active "
                "AND so.status='active' FOR UPDATE OF so",
                document_id,
                workspace_id,
            )
            if row is None:
                return False
            await conn.execute(
                "UPDATE documents SET is_active=false "
                "WHERE source_object_id=$1 AND workspace_id=$2 AND is_active",
                row["source_object_id"],
                workspace_id,
            )
            await conn.execute(
                "UPDATE document_revisions SET status='deleted' "
                "WHERE id=$1 AND workspace_id=$2",
                row["revision_id"],
                workspace_id,
            )
            await conn.execute(
                "UPDATE source_objects SET status='deleted', deleted_at=now(), updated_at=now() "
                "WHERE id=$1 AND workspace_id=$2",
                row["source_object_id"],
                workspace_id,
            )
    return True


async def ingest_document(req: IngestRequest, workspace_id: int) -> Tuple[int, bool]:
    """Accept a revision, materialize it, and queue formation when it is new."""
    document_id, revision_id, duplicate = await accept_revision(req, workspace_id)
    if duplicate:
        return document_id, True
    await _materialize_revision(req, workspace_id, document_id, revision_id)
    await schedule_formation(document_id)
    return document_id, False
