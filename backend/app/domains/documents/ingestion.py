"""Ingestion pipeline: dedup → chunk → embed → store → enqueue formation."""

import hashlib
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Tuple

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


class ClaimedMaterialization(NamedTuple):
    document_id: int
    revision_id: int
    workspace_id: int


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
                "source_connection_id, source_stream_id, external_ref, source_object_id, revision_id, formation_status"
                ") VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 'materializing') RETURNING id",
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


async def claim_materialization() -> Optional[ClaimedMaterialization]:
    """Claim one accepted revision fairly before any provider work begins."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "WITH candidate_workspace AS ("
                "  SELECT w.id FROM workspaces w WHERE EXISTS ("
                "    SELECT 1 FROM document_revisions r JOIN documents d ON d.revision_id=r.id "
                "    WHERE r.workspace_id=w.id AND d.workspace_id=w.id AND d.is_active "
                "    AND r.status='accepted' AND (r.materialization_next_attempt_at IS NULL "
                "      OR r.materialization_next_attempt_at <= now())"
                "  ) ORDER BY w.last_materialization_served_at ASC NULLS FIRST, w.id "
                "  FOR UPDATE SKIP LOCKED LIMIT 1"
                "), candidate_revision AS ("
                "  SELECT r.id AS revision_id, d.id AS document_id, r.workspace_id "
                "  FROM document_revisions r JOIN documents d ON d.revision_id=r.id "
                "  JOIN candidate_workspace w ON w.id=r.workspace_id "
                "  WHERE d.is_active AND r.status='accepted' "
                "  AND (r.materialization_next_attempt_at IS NULL OR r.materialization_next_attempt_at <= now()) "
                "  ORDER BY r.id FOR UPDATE SKIP LOCKED LIMIT 1"
                "), claimed AS ("
                "  UPDATE document_revisions r SET status='materializing', "
                "  materialization_claimed_at=now(), materialization_attempts=materialization_attempts+1 "
                "  FROM candidate_revision c WHERE r.id=c.revision_id "
                "  RETURNING c.document_id, r.id AS revision_id, r.workspace_id"
                "), served AS ("
                "  UPDATE workspaces w SET last_materialization_served_at=now() "
                "  FROM claimed c WHERE w.id=c.workspace_id"
                ") SELECT document_id, revision_id, workspace_id FROM claimed"
            )
    return ClaimedMaterialization(**dict(row)) if row is not None else None


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


async def record_materialization_failure(
    claimed: ClaimedMaterialization, error: Exception
) -> bool:
    """Release a failed worker claim with bounded exponential backoff.

    Returns whether the immutable revision was permanently failed. The claim
    count is incremented atomically by ``claim_materialization`` before a
    provider call, so a crash and a regular provider error share the same retry
    budget.
    """
    pool = await db.get_pool()
    detail = str(error)[:2000]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE document_revisions SET "
            "status=CASE WHEN materialization_attempts >= $3 THEN 'failed' ELSE 'accepted' END, "
            "error=$4, materialization_claimed_at=NULL, "
            "materialization_next_attempt_at=CASE "
            "WHEN materialization_attempts >= $3 THEN NULL "
            "ELSE now() + ($5 * power(2, GREATEST(0, materialization_attempts - 1))) * interval '1 second' "
            "END "
            "WHERE id=$1 AND workspace_id=$2 AND status='materializing' "
            "RETURNING status",
            claimed.revision_id,
            claimed.workspace_id,
            config.PREPROCESS_MAX_ATTEMPTS,
            detail,
            config.PREPROCESS_BACKOFF_S,
        )
        if row is not None and row["status"] == "failed":
            await conn.execute(
                "UPDATE documents SET formation_status='failed', formation_error=$2 "
                "WHERE id=$1 AND workspace_id=$3",
                claimed.document_id, detail, claimed.workspace_id,
            )
    return row is not None and row["status"] == "failed"


async def _materialize_revision(
    req: IngestRequest, workspace_id: int, document_id: int, revision_id: int,
    *, mark_failure: bool = True,
) -> None:
    """Embed and store chunks for an already durable accepted revision."""
    pool = await db.get_pool()
    try:
        embed_model = await active_embed_model()
        async with pool.acquire() as conn:
            model_id = await embedding_versions.ensure_model(conn, embed_model)
            workspace_model_id = await embedding_versions.active_model(conn, workspace_id)
            if workspace_model_id is None:
                # A workspace can contain manually curated decisions before its
                # first searchable document. Bootstrap the chunk model now;
                # consolidation backfills those signatures before any later
                # operator-initiated model switch is allowed.
                await embedding_versions.activate_model(
                    conn, workspace_id, model_id, require_node_coverage=False
                )
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
        if mark_failure:
            await _mark_materialization_failed(document_id, revision_id, exc)
        raise


async def materialize_claimed_revision(claimed: ClaimedMaterialization) -> bool:
    """Materialize one claimed immutable revision and then enqueue formation."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT d.id AS document_id, d.workspace_id, r.id AS revision_id, r.source, "
            "r.title, r.author, r.doc_created_at, r.raw_text, r.tags "
            "FROM documents d JOIN document_revisions r ON r.id=d.revision_id "
            "WHERE d.id=$1 AND d.workspace_id=$2 AND r.id=$3 "
            "AND r.status='materializing'",
            claimed.document_id, claimed.workspace_id, claimed.revision_id,
        )
    if row is None:
        return False
    req = IngestRequest(
        source=row["source"], title=row["title"], text=row["raw_text"],
        author=row["author"], created_at=row["doc_created_at"].isoformat()
        if row["doc_created_at"] else None,
        tags=list(row["tags"] or []),
    )
    try:
        await _materialize_revision(
            req, claimed.workspace_id, claimed.document_id, claimed.revision_id,
            mark_failure=False,
        )
    except Exception as exc:
        await record_materialization_failure(claimed, exc)
        return False
    await schedule_formation(claimed.document_id)
    return True


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
    """Durably accept a revision, materializing inline only when configured.

    Production calls return after the immutable acceptance transaction; the
    preprocessing worker owns provider calls and formation handoff. This keeps
    request latency independent from document size and embedding-provider load.
    """
    document_id, revision_id, duplicate = await accept_revision(req, workspace_id)
    if duplicate:
        return document_id, True
    if not config.INGEST_INLINE_MATERIALIZATION:
        await worker.wake_preprocessing()
        return document_id, False
    await _materialize_revision(req, workspace_id, document_id, revision_id)
    await schedule_formation(document_id)
    return document_id, False
