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
from ..memory import worker


async def schedule_formation(doc_id: int) -> None:
    await worker.enqueue(doc_id)


class IngestRequest(BaseModel):
    source: str = Field(..., description="slack | notion | github | meeting | other",
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


def content_hash(source: str, title: str, text: str) -> str:
    return hashlib.sha256(f"{source}\n{title}\n{text}".encode()).hexdigest()


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


async def ingest_document(req: IngestRequest, workspace_id: int) -> Tuple[int, bool]:
    """Returns (document_id, duplicate). Exact re-ingests (same source, title,
    and text) return the existing document instead of duplicating memory."""
    pool = await db.get_pool()
    digest = content_hash(req.source, req.title, req.text)
    async with pool.acquire() as conn:
        if req.external_ref and req.source_connection_id:
            existing = await conn.fetchval(
                "SELECT id FROM documents WHERE workspace_id=$1 "
                "AND source_connection_id=$2 AND external_ref=$3 LIMIT 1",
                workspace_id, req.source_connection_id, req.external_ref,
            )
            if existing is not None:
                return existing, True
        existing = await conn.fetchval(
            "SELECT id FROM documents WHERE workspace_id=$1 AND content_hash=$2 LIMIT 1",
            workspace_id, digest,
        )
    if existing is not None:
        return existing, True

    embed_model = await active_embed_model()
    async with pool.acquire() as conn:
        existing_models = await conn.fetch(
            "SELECT DISTINCT COALESCE(c.embed_model, 'legacy:unknown') AS embed_model "
            "FROM chunks c JOIN documents d ON d.id=c.document_id "
            "WHERE d.workspace_id=$1 AND c.embed_model IS DISTINCT FROM $2 "
            "ORDER BY 1 LIMIT 5",
            workspace_id, embed_model,
        )
    if existing_models:
        raise EmbeddingSpaceMismatch(
            embed_model, [row["embed_model"] for row in existing_models]
        )

    pieces = chunk_text(req.text)
    usage_token = usage.set_context(workspace_id=workspace_id, surface="ingest")
    try:
        embeddings = await embed_texts(pieces)
    finally:
        usage.reset_context(usage_token)
    async with pool.acquire() as conn:
        async with conn.transaction():
            doc_id = await conn.fetchval(
                "INSERT INTO documents(workspace_id, source, title, author, doc_created_at, raw_text, "
                "tags, content_hash, source_connection_id, source_stream_id, external_ref) "
                "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id",
                workspace_id, req.source, req.title, req.author, _parse_date(req.created_at),
                req.text, req.tags, digest, req.source_connection_id,
                req.source_stream_id, req.external_ref,
            )
            for i, (piece, emb) in enumerate(zip(pieces, embeddings)):
                await conn.execute(
                    "INSERT INTO chunks(document_id, chunk_index, text, embedding, embed_model) "
                    "VALUES($1, $2, $3, $4::vector, $5)",
                    doc_id, i, piece, to_pgvector(emb), embed_model,
                )
    await schedule_formation(doc_id)
    return doc_id, False
