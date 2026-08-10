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
from app.providers.embeddings import EmbeddingSpaceMismatch, active_embed_model, embed_texts, to_pgvector
from ..memory import graph
from ..memory.scoring import node_score
from . import embedding_versions, vector_search

RRF_K = 60  # standard reciprocal-rank-fusion damping constant

_DEFAULT_RELATION_PRIORITY = {
    "revisits": 0, "resolves": 1, "relates_to": 2, "raised_by": 3,
    "involves": 4, "about": 5,
}
_INTENT_RELATION_PRIORITY = {
    "decision_history": {
        "revisits": 0, "resolves": 1, "relates_to": 2, "about": 3,
        "involves": 4, "raised_by": 5,
    },
    "people": {
        "involves": 0, "raised_by": 1, "relates_to": 2, "revisits": 3,
        "resolves": 4, "about": 5,
    },
    "open_questions": {
        "resolves": 0, "relates_to": 1, "raised_by": 2, "revisits": 3,
        "involves": 4, "about": 5,
    },
}


def classify_query_intent(question: str) -> str:
    """Choose bounded traversal emphasis without a latency-prone model call."""
    words = (question or "").lower()
    if any(token in words for token in ("reversed", "reverse", "revisited", "reaffirmed", "why did", "history")):
        return "decision_history"
    if any(token in words for token in ("who", "person", "people", "owner", "advocated", "made the decision")):
        return "people"
    if any(token in words for token in ("open question", "unanswered", "unresolved", "still open", "which questions")):
        return "open_questions"
    return "general"


def relation_priority_for_intent(intent: str) -> Dict[str, int]:
    return dict(_INTENT_RELATION_PRIORITY.get(intent, _DEFAULT_RELATION_PRIORITY))


def rrf_fuse(ranked_lists: Sequence[Iterable[int]], limit: int) -> List[int]:
    """Fuse ranked id lists: score(id) = Σ 1/(RRF_K + rank). Ids appearing in
    several lists rise; order within a list matters, raw scores don't."""
    scores: Dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda i: (-scores[i], i))[:limit]


def rerank_candidate_ids(
    vector_ids: Sequence[int], text_ids: Sequence[int], limit: int
) -> List[int]:
    """Final deterministic rank over a wider semantic/lexical candidate union.

    Reciprocal-rank fusion deliberately uses ranks rather than incomparable raw
    pgvector and tsquery scores. A chunk supported by both independent paths
    therefore outranks a one-path candidate without another provider roundtrip.
    """
    return rrf_fuse([vector_ids, text_ids], limit=max(0, limit))


def select_diverse_chunks(
    chunks: Sequence[Dict[str, Any]],
    limit: int,
    per_document_cap: int,
) -> List[Dict[str, Any]]:
    """Keep high-relevance evidence while avoiding one-document context floods."""
    selected: List[Dict[str, Any]] = []
    per_document: Dict[int, int] = {}
    for chunk in sorted(
        chunks, key=lambda c: (-float(c.get("retrieval_score", 0.0)), c["id"])
    ):
        document_id = chunk["document_id"]
        if per_document.get(document_id, 0) >= max(1, per_document_cap):
            continue
        selected.append(chunk)
        per_document[document_id] = per_document.get(document_id, 0) + 1
        if len(selected) >= max(0, limit):
            break
    return selected


def _chunk_row(r) -> Dict[str, Any]:
    return {
        "id": r["id"],
        "text": r["text"],
        "document_id": r["document_id"],
        "source": r["source"],
        "title": r["title"],
        "author": r["author"],
        "date": iso_date(r["doc_created_at"]),
    }


# Graph-evidence ranking blends how much the memory can be trusted
# (node_score: status × recency) with how much the chunk bears on this
# question (embedding similarity). The similarity floor keeps trusted memory
# from vanishing entirely when its wording differs from the question — the
# graph brought it in for a structural reason (revisits/resolves), not a
# lexical one.
_EVIDENCE_SIM_FLOOR = 0.5


def rank_graph_evidence(rows: Iterable[Any], score_by_node: Dict[int, float]) -> List[Any]:
    """Order candidate (chunk, node) link rows for the context cap.

    `rows` may hold several rows per chunk (one per linked node); each chunk
    is ranked once, by its best-scoring linked node — deterministically, unlike
    the previous DISTINCT ON (id) which attributed the chunk to an arbitrary
    node. Rows need `id`, `node_id`, and `sim` (query cosine similarity)."""
    best: Dict[int, tuple] = {}
    for r in rows:
        node = score_by_node.get(r["node_id"], 0.5)
        sim = max(0.0, float(r["sim"])) if r["sim"] is not None else 0.0
        score = node * (_EVIDENCE_SIM_FLOOR + (1.0 - _EVIDENCE_SIM_FLOOR) * sim)
        cur = best.get(r["id"])
        if cur is None or score > cur[0]:
            best[r["id"]] = (score, r)
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [row for _, (_, row) in ranked]


async def retrieve(
    question: str, workspace_id: int, embed_text: Optional[str] = None
) -> Dict[str, Any]:
    pool = await db.get_pool()
    qvec = (await embed_texts([embed_text or question], kind="query"))[0]
    intent = classify_query_intent(question)
    candidate_limit = config.TOP_K * config.RETRIEVAL_CANDIDATE_MULTIPLIER
    async with pool.acquire() as conn:
        embedding_model_id = await embedding_versions.active_model(conn, workspace_id)
        if embedding_model_id is None:
            vector_result = vector_search.VectorSearchResult([], 0, False)
        else:
            embed_model = await active_embed_model()
            active_key = await embedding_versions.model_key(conn, embedding_model_id)
            if active_key != embed_model:
                raise EmbeddingSpaceMismatch(embed_model, [active_key or "unknown"])
            vector_result = await vector_search.approximate_vector_search(
                conn,
                qvec=to_pgvector(qvec),
                workspace_id=workspace_id,
                embedding_model_id=embedding_model_id,
                limit=candidate_limit,
                candidate_multiplier=config.VECTOR_CANDIDATE_MULTIPLIER,
            )
        vec_rows = vector_result.rows
        ft_rows = await conn.fetch(
            "SELECT c.id, c.text, c.document_id, d.source, d.title, d.author, "
            "       d.doc_created_at, "
            "       ts_rank_cd(c.text_tsv, websearch_to_tsquery('english', $1))::float AS score "
            "FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE c.workspace_id=$2 AND d.workspace_id=$2 AND d.is_active=true "
            "AND c.text_tsv @@ websearch_to_tsquery('english', $1) "
            "ORDER BY score DESC LIMIT $3",
            question, workspace_id, candidate_limit,
        )
        by_id = {r["id"]: r for r in vec_rows}
        by_id.update({r["id"]: r for r in ft_rows if r["id"] not in by_id})
        ranked_ids = rerank_candidate_ids(
            [r["id"] for r in vec_rows], [r["id"] for r in ft_rows], candidate_limit
        )
        seed_chunk_ids = [
            chunk["id"] for chunk in select_diverse_chunks(
                [
                    {**_chunk_row(by_id[cid]), "retrieval_score": candidate_limit - rank}
                    for rank, cid in enumerate(ranked_ids)
                ],
                limit=config.TOP_K,
                per_document_cap=config.RETRIEVAL_PER_DOCUMENT_CAP,
            )
        ]
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
            hops=config.GRAPH_HOPS, max_nodes=config.GRAPH_MAX_NODES,
            relation_priority=relation_priority_for_intent(intent),
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
            # cap bites, evidence backing high-confidence memory that also
            # bears on the question gets in first.
            extra = await conn.fetch(
                "SELECT c.id, c.text, c.document_id, d.source, "
                "       d.title, d.author, d.doc_created_at, "
                "       1 - (ce.embedding <=> $3::vector) AS sim, "
                "       cl.node_id "
                "FROM chunk_links cl "
                "JOIN chunks c ON c.id = cl.chunk_id "
                "JOIN chunk_embeddings ce ON ce.chunk_id=c.id AND ce.workspace_id=c.workspace_id "
                "JOIN documents d ON d.id = c.document_id "
                "JOIN memory_nodes n ON n.id = cl.node_id "
                "WHERE c.workspace_id=$1 AND d.workspace_id=$1 AND d.is_active=true "
                "AND n.workspace_id=$1 "
                "AND cl.node_id = ANY($2::int[]) AND n.kind IN ('decision', 'question') "
                "AND n.archived_at IS NULL AND ce.embedding_model_id=$4",
                workspace_id, list(node_ids), to_pgvector(qvec), embedding_model_id,
            )
            total_chars = sum(len(c["text"]) for c in chunks.values())
            for r in rank_graph_evidence(extra, score_by_node):
                if (len(chunks) >= config.CONTEXT_CHUNK_CAP
                        or total_chars >= config.CONTEXT_CHAR_BUDGET):
                    break
                if r["id"] not in chunks:
                    chunks[r["id"]] = _chunk_row(r)
                    total_chars += len(r["text"])
                    graph_chunks += 1

    ordered = sorted(chunks.values(), key=lambda c: (c["date"] or "9999", c["id"]))
    trace = {
        "seed_chunks": len(seed_chunk_ids),
        "vector_seeds": len(vec_rows),
        "vector_candidates_scanned": vector_result.candidates_scanned,
        "hnsw_iterative_scan": vector_result.iterative_scan_enabled,
        "text_seeds": len(ft_rows),
        "graph_chunks": graph_chunks,
        "intent": intent,
        "nodes": [
            {"id": n["id"], "kind": n["kind"], "label": n["label"], "status": n["status"]}
            for n in nodes if n["kind"] in ("decision", "question")
        ][:12],
        "entities": [n["label"] for n in nodes if n["kind"] == "entity"][:10],
        "edges": len(edges),
    }
    return {"chunks": ordered, "nodes": nodes, "edges": edges, "trace": trace}
