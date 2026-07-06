from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from app.core import db
from app.core.ratelimit import ingest_limiter
from app.domains.auth import service as auth
from app.domains.documents.ingestion import IngestRequest, ingest_document, schedule_formation

router = APIRouter(prefix="/api", tags=["documents"])


@router.post("/ingest")
async def ingest(
    req: IngestRequest,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    await ingest_limiter.enforce((current.workspace_id, current.user_id), "ingest")
    doc_id, duplicate = await ingest_document(req, workspace_id=current.workspace_id)
    return {
        "document_id": doc_id,
        "duplicate": duplicate,
        "formation": "skipped" if duplicate else "scheduled",
    }


@router.post("/relink")
async def relink(
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    """Re-run memory formation across the whole corpus, oldest first, so links
    can form regardless of original ingestion order (bulk imports)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM documents WHERE workspace_id=$1 "
            "ORDER BY doc_created_at NULLS LAST, id",
            current.workspace_id,
        )
        await conn.execute(
            "UPDATE documents SET formation_status='pending', formation_error=NULL, "
            "formation_attempts=0, formation_next_attempt_at=now() "
            "WHERE workspace_id=$1",
            current.workspace_id,
        )
        await auth.audit(conn, "relink", current.workspace_id, current.user_id)
    for r in rows:
        await schedule_formation(r["id"])
    return {"requeued": len(rows)}


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    """Delete a document and any memory nodes left without evidence."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # nodes evidenced by this document (topics/implicit people never
            # carry chunk links, so they are not candidates here)
            candidates = [
                r["id"] for r in await conn.fetch(
                    "SELECT DISTINCT cl.node_id AS id FROM chunk_links cl "
                    "JOIN chunks c ON c.id = cl.chunk_id "
                    "JOIN documents d ON d.id = c.document_id "
                    "WHERE c.document_id = $1 AND d.workspace_id=$2",
                    doc_id, current.workspace_id,
                )
            ]
            row = await conn.fetchrow(
                "DELETE FROM documents WHERE id=$1 AND workspace_id=$2 RETURNING id",
                doc_id, current.workspace_id,
            )
            if row is None:
                raise HTTPException(404, "document not found")
            # of those, drop the ones whose only evidence was this document
            orphans = await conn.fetch(
                "DELETE FROM memory_nodes n WHERE n.id = ANY($1::int[]) AND NOT EXISTS "
                "(SELECT 1 FROM chunk_links cl WHERE cl.node_id = n.id) "
                "AND n.workspace_id=$2 RETURNING n.id",
                candidates, current.workspace_id,
            )
            # garbage-collect topics/entities left with no edges and no evidence
            dangling = await conn.fetch(
                "DELETE FROM memory_nodes n WHERE n.kind IN ('topic', 'entity') "
                "AND n.workspace_id=$1 "
                "AND NOT EXISTS (SELECT 1 FROM chunk_links cl WHERE cl.node_id = n.id) "
                "AND NOT EXISTS (SELECT 1 FROM memory_edges e WHERE e.src = n.id OR e.dst = n.id) "
                "RETURNING n.id",
                current.workspace_id,
            )
            await auth.audit(conn, "delete_document", current.workspace_id, current.user_id,
                             "document", doc_id)
    return {"deleted": doc_id, "orphaned_memory_removed": len(orphans) + len(dangling)}


@router.post("/documents/{doc_id}/reform")
async def reform_document(
    doc_id: int,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("admin")),
) -> Dict[str, Any]:
    """Re-run memory formation on an already-ingested document."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM documents WHERE id=$1 AND workspace_id=$2",
            doc_id, current.workspace_id,
        )
        if row is None:
            raise HTTPException(404, "document not found")
        await conn.execute(
            "UPDATE documents SET formation_attempts=0 WHERE id=$1 AND workspace_id=$2",
            doc_id, current.workspace_id,
        )
        await auth.audit(conn, "reform_document", current.workspace_id, current.user_id,
                         "document", doc_id)
    await schedule_formation(doc_id)
    return {"document_id": doc_id, "formation": "scheduled"}


@router.get("/documents")
async def list_documents(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, source, title, author, doc_created_at, tags, context_summary, "
            "       formation_status, formation_error, formation_attempts, ingested_at, "
            "       source_connection_id, source_stream_id, external_ref "
            "FROM documents WHERE workspace_id=$1 ORDER BY doc_created_at NULLS LAST, id",
            current.workspace_id,
        )
    return [dict(r) for r in rows]


@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: int,
    full: bool = False,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        doc = await conn.fetchrow(
            "SELECT * FROM documents WHERE id=$1 AND workspace_id=$2",
            doc_id, current.workspace_id,
        )
        if doc is None:
            raise HTTPException(404, "document not found")
        counts = await conn.fetch(
            "SELECT n.kind, count(DISTINCT n.id) AS n FROM memory_nodes n "
            "JOIN chunk_links cl ON cl.node_id = n.id "
            "JOIN chunks c ON c.id = cl.chunk_id "
            "WHERE c.document_id = $1 AND n.workspace_id=$2 "
            "AND n.archived_at IS NULL GROUP BY n.kind",
            doc_id, current.workspace_id,
        )
    out = dict(doc)
    raw = out.pop("raw_text", "") or ""
    if full:
        out["text"] = raw
    out["text_preview"] = raw[:1200] + ("…" if len(raw) > 1200 else "")
    out["memory_counts"] = {r["kind"]: r["n"] for r in counts}
    return out
