"""Machine-facing agent API (/api/agent/*): the same retrieval + memory-graph
engine that powers the human chat UI, exposed as structured JSON for AI agents
(coding agents, PR reviewers, IDE assistants) that need verified company
context *before acting*.

Same engine, different interface: humans get streamed markdown and UI cards;
agents get one JSON object with the answer, enriched citations, confidence,
and deterministic warnings. Conflict/freshness signals are not an LLM opinion —
they come straight from node status (reversed / revisited) and the revisits /
resolves edges that formation already maintains.

Auth is a workspace API key (auth.require_api_key) — no user session. Reads
are direct; the one write path (/propose) never touches the memory graph:
proposals queue in memory_proposals until a human curator approves them via
/api/memory-review, so an agent cannot alter live memory on its own.
"""

import logging
import re
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


class ProposeRequest(BaseModel):
    kind: str = "decision"
    label: str
    summary: str
    status: Optional[str] = None
    topics: List[str]
    data: Optional[Dict[str, Any]] = None


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


# ── Topic scoping (fine-grained key permissions) ─────────────────────────────
# A key with allowed_topics only sees memory reachable from those topics:
# the topic nodes themselves, decisions/questions with an 'about' edge to
# them, and entities linked (involves / raised_by) to those in-scope nodes.
# Everything is filtered at the decision level so nothing leaks through
# retrieval, graph expansion, or evidence chunks.


async def _allowed_node_ids(conn, current: auth.AgentContext,
                            node_ids: List[int]) -> Optional[set]:
    """None = key is unrestricted. Otherwise the subset of node_ids in scope."""
    if current.allowed_topics is None:
        return None
    if not node_ids:
        return set()
    lowered = [t.strip().lower() for t in current.allowed_topics if t.strip()]
    rows = await conn.fetch(
        "SELECT n.id FROM memory_nodes n "
        "WHERE n.id = ANY($1::int[]) AND n.workspace_id=$3 AND ("
        "  (n.kind='topic' AND lower(n.label) = ANY($2::text[])) "
        "  OR EXISTS (SELECT 1 FROM memory_edges e "
        "             JOIN memory_nodes t ON t.id=e.dst "
        "             WHERE e.src=n.id AND e.relation='about' "
        "             AND t.kind='topic' AND lower(t.label) = ANY($2::text[])) "
        "  OR (n.kind='entity' AND EXISTS ("
        "        SELECT 1 FROM memory_edges e1 "
        "        JOIN memory_edges e2 ON e2.src = e1.src AND e2.relation='about' "
        "        JOIN memory_nodes t ON t.id = e2.dst "
        "        WHERE e1.dst = n.id AND e1.relation IN ('involves', 'raised_by') "
        "        AND t.kind='topic' AND lower(t.label) = ANY($2::text[])))"
        ")",
        node_ids, lowered, current.workspace_id,
    )
    return {r["id"] for r in rows}


async def _scope_retrieval(conn, current: auth.AgentContext,
                           ret: Dict[str, Any]) -> Dict[str, Any]:
    """Filter a retrieval result down to the key's topic scope: nodes, edges
    between surviving nodes, chunks with evidence links to surviving nodes,
    and the trace summaries rebuilt from what survived."""
    allowed = await _allowed_node_ids(conn, current, [n["id"] for n in ret["nodes"]])
    if allowed is None:
        return ret
    nodes = [n for n in ret["nodes"] if n["id"] in allowed]
    edges = [e for e in ret["edges"] if e["src"] in allowed and e["dst"] in allowed]
    chunk_ids = [c["id"] for c in ret["chunks"]]
    linked = set()
    if chunk_ids and allowed:
        rows = await conn.fetch(
            "SELECT DISTINCT chunk_id FROM chunk_links "
            "WHERE chunk_id = ANY($1::int[]) AND node_id = ANY($2::int[])",
            chunk_ids, list(allowed),
        )
        linked = {r["chunk_id"] for r in rows}
    chunks = [c for c in ret["chunks"] if c["id"] in linked]
    trace = dict(ret["trace"])
    trace["nodes"] = [
        {"id": n["id"], "kind": n["kind"], "label": n["label"], "status": n["status"]}
        for n in nodes if n["kind"] in ("decision", "question")
    ][:12]
    trace["entities"] = [n["label"] for n in nodes if n["kind"] == "entity"][:10]
    trace["scoped_to_topics"] = sorted(t.lower() for t in current.allowed_topics)
    return {"chunks": chunks, "nodes": nodes, "edges": edges, "trace": trace}


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
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            ret = await _scope_retrieval(conn, current, ret)
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
        allowed = await _allowed_node_ids(conn, current, [r["id"] for r in rows])
    results = []
    for r in rows:
        if allowed is not None and r["id"] not in allowed:
            continue
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
        allowed = await _allowed_node_ids(conn, current, [node_id])
        if allowed is not None and node_id not in allowed:
            raise HTTPException(404, "decision not found")  # don't leak existence
        decision = await view_service._build_decision(conn, row, current.workspace_id)
        if allowed is not None and decision.get("related"):
            rel_allowed = await _allowed_node_ids(
                conn, current, [r["node_id"] for r in decision["related"]])
            decision["related"] = [
                r for r in decision["related"] if r["node_id"] in rel_allowed]
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


async def _briefing(current: auth.AgentContext, query: str) -> Dict[str, Any]:
    """Deterministic pre-action briefing (retrieval + graph only, no LLM),
    shared by /context (task text) and /context-for-file (derived path terms)."""
    token = usage.set_context(workspace_id=current.workspace_id, surface="agent")
    try:
        ret = await retrieval.retrieve(query, workspace_id=current.workspace_id)
    finally:
        usage.reset_context(token)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        ret = await _scope_retrieval(conn, current, ret)
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
        "relevant_decisions": decisions,
        "warnings": warnings,
        "open_questions": open_questions,
        "people": ret["trace"].get("entities", []),
        "sources": sources[:10],
        "trace": ret["trace"],
    }


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
    return {"task": task, **await _briefing(current, query)}


# Path segments that name code layout, not domain concepts — they'd only add
# noise to retrieval. Includes common file extensions long enough to survive
# the length-3 cutoff.
_PATH_NOISE = {
    "src", "lib", "app", "apps", "packages", "pkg", "internal", "public",
    "components", "component", "pages", "views", "utils", "util", "helpers",
    "helper", "hooks", "core", "common", "shared", "base", "types", "models",
    "index", "main", "test", "tests", "spec", "specs", "mock", "mocks",
    "dist", "build", "node_modules", "vendor", "__init__", "__main__",
    "json", "yaml", "toml", "html", "jsx", "tsx", "svelte", "test.js",
}


def derive_path_terms(path: str) -> List[str]:
    """Deterministically mine domain terms from a file path: split on
    separators / dots / dashes / underscores / camelCase, drop layout noise
    ("src", "components") and extensions, dedupe preserving order (directory
    names come before the filename, so broader context ranks first)."""
    terms: List[str] = []
    for segment in re.split(r"[\\/]+", path.strip()):
        for word in re.split(r"[-_.]+", segment):
            for part in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", word).split():
                part = part.lower()
                if len(part) >= 3 and part not in _PATH_NOISE and not part.isdigit():
                    terms.append(part)
    return list(dict.fromkeys(terms))[:8]


class FileContextRequest(BaseModel):
    path: str
    repo: Optional[str] = None


@router.post("/context-for-file")
async def agent_context_for_file(
    req: FileContextRequest,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    """Decision history for the file an agent is about to touch — designed for
    IDE agents to call on file-open. The path is mined for domain terms
    (src/billing/charge.ts → billing, charge) and fed through the same
    deterministic briefing as /context; derived_terms shows why results
    matched. No LLM call."""
    path = req.path.strip()
    if not path:
        raise HTTPException(400, "path must not be empty")
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent file context")
    terms = derive_path_terms(path)
    if not terms:
        return {
            "path": path, "derived_terms": [], "relevant_decisions": [],
            "warnings": [], "open_questions": [], "people": [], "sources": [],
            "trace": {"note": "path yielded no searchable domain terms"},
        }
    query = " ".join(terms)
    if req.repo and req.repo.strip():
        query = f"{req.repo.strip()} {query}"
    return {"path": path, "derived_terms": terms, **await _briefing(current, query)}


PROPOSAL_KINDS = {"decision", "question"}
PROPOSAL_STATUSES = {"pending", "approved", "rejected"}
MAX_SUMMARY_LEN = 4000
MAX_TOPICS = 8


def _proposal_out(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "proposal_id": r["id"],
        "kind": r["kind"],
        "label": r["label"],
        "summary": r["summary"],
        "status_suggestion": r["status_suggestion"],
        "topics": list(r["topics"] or []),
        "data": r["data"] or {},
        "status": r["status"],
        "resolution_note": r["resolution_note"],
        "created_node_id": r["created_node_id"],
        "created_at": r["created_at"].isoformat(),
        "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
    }


@router.post("/propose")
async def agent_propose(
    req: ProposeRequest,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    """Propose a new decision (or open question) for the memory graph. The
    proposal queues for human curation — it is NOT live memory and will not
    appear in search/ask/context until a curator approves it."""
    from app.domains.memory import review_service

    kind = (req.kind or "decision").strip().lower()
    if kind not in PROPOSAL_KINDS:
        raise HTTPException(400, "kind must be decision or question")
    label = " ".join((req.label or "").split())[:300]
    if not label:
        raise HTTPException(400, "label is required")
    summary = (req.summary or "").strip()
    if not summary:
        raise HTTPException(400, "summary is required")
    if len(summary) > MAX_SUMMARY_LEN:
        raise HTTPException(400, f"summary exceeds {MAX_SUMMARY_LEN} characters")
    status = (req.status or "").strip() or None
    if status is not None:
        allowed = (review_service.DECISION_STATUSES if kind == "decision"
                   else review_service.QUESTION_STATUSES)
        if status not in allowed:
            raise HTTPException(400, f"invalid {kind} status '{status}'")
    topics = [t.strip().lower() for t in (req.topics or []) if t and t.strip()]
    topics = list(dict.fromkeys(topics))[:MAX_TOPICS]
    if not topics:
        raise HTTPException(400, "at least one topic is required")
    if current.allowed_topics is not None:
        scope = {t.strip().lower() for t in current.allowed_topics}
        outside = [t for t in topics if t not in scope]
        if outside:
            raise HTTPException(
                403,
                f"this API key is scoped to topics {sorted(scope)} and cannot "
                f"propose under {outside}")

    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent propose")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO memory_proposals(workspace_id, api_key_id, kind, label, "
                "summary, status_suggestion, topics, data) "
                "VALUES($1, $2, $3, $4, $5, $6, $7, $8) RETURNING *",
                current.workspace_id, current.key_id, kind, label, summary,
                status, topics, req.data or {},
            )
            await auth.audit(
                conn, "agent_proposal_created", current.workspace_id, None,
                "memory_proposal", row["id"],
                {"key_id": current.key_id, "key_name": current.key_name,
                 "kind": kind, "label": label},
            )
        # Same active-label check the approval upsert will hit — surfacing it
        # now lets the agent know its proposal will merge, not create.
        existing = await conn.fetchrow(
            "SELECT id, label, status FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind=$2 AND lower(label)=lower($3) "
            "AND archived_at IS NULL",
            current.workspace_id, kind, label,
        )
    out = _proposal_out(dict(row))
    if existing:
        out["warnings"] = [{
            "type": "existing_node",
            "node_id": existing["id"],
            "message": (f"An active {kind} '{existing['label']}' already exists — "
                        "approval will merge into it rather than create a new node."),
        }]
    return out


@router.get("/proposals")
async def agent_proposals(
    status: Optional[str] = None,
    limit: int = 50,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> List[Dict[str, Any]]:
    """List this workspace's proposals, newest first, so an agent can track
    what happened to its submissions."""
    if status and status not in PROPOSAL_STATUSES:
        raise HTTPException(400, "status must be pending, approved, or rejected")
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent proposals")
    limit = max(1, min(limit, 100))
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM memory_proposals WHERE workspace_id=$1 AND status=$2 "
                "ORDER BY created_at DESC LIMIT $3",
                current.workspace_id, status, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM memory_proposals WHERE workspace_id=$1 "
                "ORDER BY created_at DESC LIMIT $2",
                current.workspace_id, limit,
            )
    return [_proposal_out(dict(r)) for r in rows]


@router.get("/proposals/{proposal_id}")
async def agent_proposal(
    proposal_id: int,
    current: auth.AgentContext = Depends(auth.require_api_key),
) -> Dict[str, Any]:
    await agent_limiter.enforce((current.workspace_id, current.key_id), "agent proposal")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM memory_proposals WHERE id=$1 AND workspace_id=$2",
            proposal_id, current.workspace_id,
        )
    if row is None:
        raise HTTPException(404, "proposal not found")
    return _proposal_out(dict(row))
