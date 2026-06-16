"""Graph-aware hybrid retrieval.

Seeds come from two searches fused with reciprocal-rank fusion: vector
similarity (semantic match) and Postgres full-text (exact terms — ticket IDs,
names — that embeddings miss). The fused top-k chunks then seed the memory
graph (via chunk_links), the graph is expanded over typed edges (revisits /
resolves / involves / about / relates_to), and evidence chunks for the
discovered decision/question nodes are pulled back in — surfacing sources that
neither search alone would find (e.g. the Jira ticket that almost reversed a
decision)."""

from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.core import config, db
from app.core.dates import iso_date
from app.providers.embeddings import embed_texts, to_pgvector
from ..memory import graph
from ..memory.scoring import node_score

RRF_K = 60  # standard reciprocal-rank-fusion damping constant


def rrf_fuse(ranked_lists: Sequence[Iterable[int]], limit: int) -> List[int]:
    """Fuse ranked id lists: score(id) = Σ 1/(RRF_K + rank). Ids appearing in
    several lists rise; order within a list matters, raw scores don't."""
    scores: Dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda i: (-scores[i], i))[:limit]


def _chunk_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "text": r["text"],
        "document_id": r["document_id"],
        "source": r["source"],
        "title": r["title"],
        "author": r["author"],
        "date": iso_date(r["doc_created_at"]),
        "score": float(r["score"]) if r["score"] is not None else None,
    }


async def retrieve(
    question: str, workspace_id: int, embed_text: Optional[str] = None
) -> Dict[str, Any]:
    pool = await db.get_pool()
    qvec = (await embed_texts([embed_text or question], kind="query"))[0]
    async with pool.acquire() as conn:
        vec_rows = await conn.fetch(
            "SELECT c.id, c.text, c.document_id, d.source, d.title, d.author, "
            "       d.doc_created_at, 1 - (c.embedding <=> $1::vector) AS score "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.workspace_id=$2 "
            "ORDER BY c.embedding <=> $1::vector LIMIT $3",
            to_pgvector(qvec), workspace_id, config.TOP_K,
        )
        ft_rows = await conn.fetch(
            "SELECT c.id, c.text, c.document_id, d.source, d.title, d.author, "
            "       d.doc_created_at, "
            "       ts_rank_cd(c.text_tsv, websearch_to_tsquery('english', $1))::float AS score "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.workspace_id=$2 AND c.text_tsv @@ websearch_to_tsquery('english', $1) "
            "ORDER BY score DESC LIMIT $3",
            question, workspace_id, config.TOP_K,
        )
        by_id = {r["id"]: r for r in vec_rows}
        by_id.update({r["id"]: r for r in ft_rows if r["id"] not in by_id})
        seed_chunk_ids = rrf_fuse(
            [[r["id"] for r in vec_rows], [r["id"] for r in ft_rows]], config.TOP_K
        )
        chunks: Dict[int, Dict[str, Any]] = {
            cid: _chunk_row(by_id[cid]) for cid in seed_chunk_ids
        }

        seed_rows = await conn.fetch(
            "SELECT DISTINCT cl.node_id FROM chunk_links cl "
            "JOIN memory_nodes n ON n.id=cl.node_id "
            "WHERE cl.chunk_id = ANY($1::int[]) AND n.workspace_id=$2 "
            "AND n.archived_at IS NULL",
            seed_chunk_ids, workspace_id,
        )
        seed_ids = [r["node_id"] for r in seed_rows]
        node_ids, edges = await graph.expand(
            conn, workspace_id, seed_ids,
            hops=config.GRAPH_HOPS, max_nodes=config.GRAPH_MAX_NODES
        )

        nodes: List[Dict[str, Any]] = []
        graph_chunks = 0
        if node_ids:
            node_rows = await conn.fetch(
                "SELECT id, kind, label, summary, status, data FROM memory_nodes "
                "WHERE workspace_id=$1 AND id = ANY($2::int[]) "
                "AND archived_at IS NULL ORDER BY kind, id",
                workspace_id, list(node_ids),
            )
            nodes = [dict(r) for r in node_rows]
            score_by_node = {
                n["id"]: node_score(n["status"], n["data"])
                for n in nodes if n["kind"] in ("decision", "question")
            }

            # Pull evidence chunks for decision/question nodes found via the
            # graph — this is the "memory, not search" step. When the context
            # cap bites, higher-confidence memory gets its evidence in first.
            extra = await conn.fetch(
                "SELECT DISTINCT ON (c.id) c.id, c.text, c.document_id, d.source, "
                "       d.title, d.author, d.doc_created_at, NULL::float AS score, "
                "       cl.node_id "
                "FROM chunk_links cl "
                "JOIN chunks c ON c.id = cl.chunk_id "
                "JOIN documents d ON d.id = c.document_id "
                "JOIN memory_nodes n ON n.id = cl.node_id "
                "WHERE d.workspace_id=$1 AND n.workspace_id=$1 "
                "AND cl.node_id = ANY($2::int[]) AND n.kind IN ('decision', 'question') "
                "AND n.archived_at IS NULL "
                "ORDER BY c.id",
                workspace_id, list(node_ids),
            )
            ranked = sorted(
                extra, key=lambda r: -score_by_node.get(r["node_id"], 0.5)
            )
            for r in ranked:
                if len(chunks) >= config.CONTEXT_CHUNK_CAP:
                    break
                if r["id"] not in chunks:
                    chunks[r["id"]] = _chunk_row(r)
                    graph_chunks += 1

    ordered = sorted(chunks.values(), key=lambda c: (c["date"] or "9999", c["id"]))
    trace = {
        "seed_chunks": len(seed_chunk_ids),
        "vector_seeds": len(vec_rows),
        "text_seeds": len(ft_rows),
        "graph_chunks": graph_chunks,
        "nodes": [
            {"id": n["id"], "kind": n["kind"], "label": n["label"], "status": n["status"]}
            for n in nodes if n["kind"] in ("decision", "question")
        ][:12],
        "entities": [n["label"] for n in nodes if n["kind"] == "entity"][:10],
        "edges": len(edges),
    }
    return {"chunks": ordered, "nodes": nodes, "edges": edges, "trace": trace}
