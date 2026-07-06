"""Machine-facing agent API (/api/agent/*): the same retrieval + memory-graph
engine that powers the human chat UI, exposed as structured JSON for AI agents
(coding agents, PR reviewers, IDE assistants) that need verified company
context *before acting*.

Same engine, different interface: humans get streamed markdown and UI cards;
agents get one JSON object with the answer, enriched citations, confidence,
and deterministic warnings. Conflict/freshness signals are not an LLM opinion —
they come straight from node status (reversed / revisited) and the revisits /
resolves edges that formation already maintains.

Auth is a workspace API key (auth.require_api_key) — no user session. All
endpoints are read-only in v1.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import db, usage
from app.core.dates import iso_date
from app.core.ratelimit import agent_limiter
from app.domains.auth import service as auth
from app.domains.memory import view_service
from app.domains.memory.scoring import node_score
from app.domains.query import retrieval
from app.domains.query.streaming import build_context, locate_quote
from app.providers import llm

log = logging.getLogger("ybase.agent")

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AskRequest(BaseModel):
    question: str


class ContextRequest(BaseModel):
    task: str
    topics: Optional[List[str]] = None


AGENT_SYSTEM = """You are a company's institutional memory — its "YBase" — answering \
a question asked programmatically by an AI agent that is about to perform a task. \
Use ONLY the memory provided: source chunks (primary evidence) and the memory graph \
(distilled decisions, entities, questions and their typed relationships). \
Today's date is {today}.

How to reason:
- Identify which decisions in memory are relevant to the question.
- Check whether anything was revisited, reversed, or reaffirmed later — use graph edges \
(revisits, resolves, relates_to) and document dates to build the chronology.
- Attribute positions to specific people when the sources support it.
- Be explicit about what memory does NOT contain. Never invent facts, people, or dates.

Output rules:
- `answer`: plain markdown prose (no tables, no headings), citing every factual claim \
inline with chunk markers like [C12]. If memory cannot answer, say so plainly and point \
to the nearest related memory.
- `takeaway`: one short sentence, no citations.
- `confidence`: how well the retrieved memory supports the answer.
- `citations`: for each chunk you cited, the exact words copied verbatim from that chunk \
that back the claim — the shortest 1-2 sentence span, character-for-character, no \
paraphrasing and no ellipsis."""

AGENT_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "takeaway": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["chunk_id", "quote"],
            },
        },
    },
    "required": ["answer", "takeaway", "confidence", "citations"],
}


def derive_signals(
    nodes: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deterministic conflict/freshness signals from retrieved graph nodes —
    no LLM involved. A reversed decision in the neighborhood is exactly the
    thing an agent must not build on; an open question is exactly the thing
    it should flag instead of silently resolving."""
    warnings: List[Dict[str, Any]] = []
    open_questions: List[Dict[str, Any]] = []
    for n in nodes:
        if n["kind"] == "decision" and n["status"] == "reversed":
            warnings.append({
                "type": "reversed_decision",
                "node_id": n["id"],
                "message": (f"Decision '{n['label']}' was later REVERSED — "
                            "do not treat it as current policy."),
            })
        elif n["kind"] == "decision" and n["status"] == "revisited":
            warnings.append({
                "type": "revisited_decision",
                "node_id": n["id"],
                "message": (f"Decision '{n['label']}' has been revisited — "
                            "check its successors before relying on it."),
            })
        elif n["kind"] == "question" and n["status"] == "open":
            open_questions.append({"node_id": n["id"], "question": n["label"]})
    return warnings, open_questions


def _node_summary(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": n["id"],
        "kind": n["kind"],
        "label": n["label"],
        "status": n["status"],
        "confidence": round(node_score(n["status"], n.get("data")), 3),
    }


def _enrich_citations(
    raw: List[Dict[str, Any]], chunks: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Join the model's {chunk_id, quote} pairs against the retrieved chunks,
    dropping hallucinated ids and verifying quotes verbatim — the same
    discipline as the chat metadata path."""
    by_id = {c["id"]: c for c in chunks}
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw or []:
        cid = item.get("chunk_id")
        chunk = by_id.get(cid)
        if chunk is None or cid in seen:
            continue
        seen.add(cid)
        out.append({
            "chunk_id": cid,
            "document_id": chunk["document_id"],
            "source": chunk["source"],
            "title": chunk["title"],
            "author": chunk["author"],
            "date": chunk["date"],
            "snippet": (chunk["text"] or "")[:240],
            "quote": locate_quote(chunk["text"], item.get("quote")),
        })
    return out


@router.post("/ask")
async def agent_ask(
    req: AskRequest,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    """Structured, evidence-backed answer to one question. Non-streaming
    equivalent of the chat query, plus deterministic warnings."""
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question must not be empty")
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent ask")
    token = usage.set_context(workspace_id=current.workspace_id, surface="agent")
    try:
        ret = await retrieval.retrieve(question, workspace_id=current.workspace_id)
        context = build_context(question, ret)
        system = AGENT_SYSTEM.format(today=date.today().isoformat())
        try:
            out = await llm.structured_call(system, context, AGENT_ANSWER_SCHEMA)
        except Exception as e:
            log.exception("agent ask LLM call failed workspace=%s", current.workspace_id)
            raise HTTPException(502, f"LLM call failed: {e}")
    finally:
        usage.reset_context(token)
    warnings, open_questions = derive_signals(ret["nodes"])
    return {
        "answer": out.get("answer") or "",
        "takeaway": out.get("takeaway") or "",
        "confidence": out.get("confidence") or "unknown",
        "citations": _enrich_citations(out.get("citations"), ret["chunks"]),
        "warnings": warnings,
        "open_questions": open_questions,
        "nodes": [_node_summary(n) for n in ret["nodes"]
                  if n["kind"] in ("decision", "question")],
        "trace": ret["trace"],
    }


@router.get("/search")
async def agent_search(
    q: str,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> List[Dict[str, Any]]:
    """Lexical search over memory nodes with per-node confidence. For semantic
    question-answering use /ask; this is for locating specific nodes."""
    q = q.strip()
    if not q:
        return []
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent search")
    limit = max(1, min(limit, 50))
    like = f"%{q}%"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if kind:
            rows = await conn.fetch(
                "SELECT id, kind, label, summary, status, data FROM memory_nodes "
                "WHERE workspace_id=$1 AND archived_at IS NULL AND kind=$3 "
                "AND (label ILIKE $2 OR summary ILIKE $2) "
                "ORDER BY (label ILIKE $2) DESC, updated_at DESC LIMIT $4",
                current.workspace_id, like, kind, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, kind, label, summary, status, data FROM memory_nodes "
                "WHERE workspace_id=$1 AND archived_at IS NULL "
                "AND (label ILIKE $2 OR summary ILIKE $2) "
                "ORDER BY (label ILIKE $2) DESC, updated_at DESC LIMIT $3",
                current.workspace_id, like, limit,
            )
    results = []
    for r in rows:
        if status and r["status"] != status:
            continue
        results.append({
            "id": r["id"],
            "kind": r["kind"],
            "label": r["label"],
            "summary": (r["summary"] or "")[:280],
            "status": r["status"],
            "confidence": round(node_score(r["status"], r["data"]), 3),
        })
    return results


@router.get("/decisions/{node_id}")
async def agent_decision(
    node_id: int,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    """One decision with its full evidence chain: reasoning, people, sources,
    evidence excerpts, and explicit supersession links (revisits/resolves)."""
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent decision")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, label, summary, status, data, created_at, updated_at "
            "FROM memory_nodes WHERE id=$1 AND workspace_id=$2 AND kind='decision' "
            "AND archived_at IS NULL",
            node_id, current.workspace_id,
        )
        if row is None:
            raise HTTPException(404, "decision not found")
        decision = await view_service._build_decision(conn, row, current.workspace_id)
        evidence = await conn.fetch(
            "SELECT c.id AS chunk_id, c.text, d.id AS document_id, d.source, d.title, "
            "d.author, d.doc_created_at "
            "FROM chunk_links cl JOIN chunks c ON c.id = cl.chunk_id "
            "JOIN documents d ON d.id = c.document_id "
            "WHERE cl.node_id=$1 AND d.workspace_id=$2 "
            "ORDER BY d.doc_created_at NULLS LAST LIMIT 12",
            node_id, current.workspace_id,
        )
    # Formation writes revisits edges new→old, so an outgoing revisits edge
    # means this decision supersedes the target; incoming means the reverse.
    supersedes = [r for r in decision["related"]
                  if r["relation"] == "revisits" and r["direction"] == "out"]
    superseded_by = [r for r in decision["related"]
                     if r["relation"] == "revisits" and r["direction"] == "in"]
    resolves = [r for r in decision["related"]
                if r["relation"] == "resolves" and r["direction"] == "out"]
    decision.update({
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "resolves": resolves,
        "evidence": [
            {
                "chunk_id": e["chunk_id"],
                "document_id": e["document_id"],
                "source": e["source"],
                "title": e["title"],
                "author": e["author"],
                "date": iso_date(e["doc_created_at"]),
                "text": (e["text"] or "")[:1200],
            }
            for e in evidence
        ],
    })
    return decision


@router.post("/context")
async def agent_context(
    req: ContextRequest,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    """'What should I know before doing X' — the pre-action briefing. Fully
    deterministic (retrieval + graph only, no LLM call): fast enough to sit in
    an agent's inner loop, and every field is traceable to stored memory."""
    task = req.task.strip()
    if not task:
        raise HTTPException(400, "task must not be empty")
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent context")
    query = task
    if req.topics:
        query += " " + " ".join(t.strip() for t in req.topics if t.strip())
    token = usage.set_context(workspace_id=current.workspace_id, surface="agent")
    try:
        ret = await retrieval.retrieve(query, workspace_id=current.workspace_id)
    finally:
        usage.reset_context(token)
    warnings, open_questions = derive_signals(ret["nodes"])
    decisions = []
    for n in ret["nodes"]:
        if n["kind"] != "decision":
            continue
        data = n.get("data") or {}
        decisions.append({
            "node_id": n["id"],
            "title": n["label"],
            "summary": n["summary"],
            "status": n["status"],
            "confidence": round(node_score(n["status"], data), 3),
            "date": data.get("date"),
            "made_by": data.get("made_by", []),
        })
    seen_docs = set()
    sources = []
    for c in ret["chunks"]:
        key = (c["source"], c["title"])
        if key in seen_docs:
            continue
        seen_docs.add(key)
        sources.append({
            "document_id": c["document_id"],
            "source": c["source"],
            "title": c["title"],
            "date": c["date"],
        })
    return {
        "task": task,
        "relevant_decisions": decisions,
        "warnings": warnings,
        "open_questions": open_questions,
        "people": ret["trace"].get("entities", []),
        "sources": sources[:10],
        "trace": ret["trace"],
    }
