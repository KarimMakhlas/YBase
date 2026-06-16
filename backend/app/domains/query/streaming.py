"""Query engine: graph-aware retrieval + Claude reasoning, streamed as SSE.

Claude streams a markdown answer with [C<id>] citations, then a metadata block
(confidence, cited chunk ids, related questions, timeline) after a delimiter.
The answer streams to the client token by token; the metadata is buffered,
parsed, enriched with provenance, and emitted as a final event."""

import json
import logging
import re
from datetime import date
from typing import Any, AsyncIterator, Dict, List, Optional

from app.providers import llm
from app.core.observability import StageTimer
from . import retrieval

DELIM = "<<<MEMORY_METADATA>>>"
_CITE_RE = re.compile(r"\[C(\d+)\]")
log = logging.getLogger("whybase.query")

# Follow-ups shorter than this likely lean on the conversation ("why did he
# push back?") and retrieve weakly verbatim — rewrite them standalone first.
FOLLOWUP_MAX_CHARS = 100

REWRITE_SYSTEM = """You rewrite the user's latest follow-up question so it stands alone \
without the conversation. Resolve pronouns and ellipsis into the concrete people, \
decisions, and topics they refer to. Keep it one question, keep the user's intent \
exactly — never answer it, never add new constraints."""

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"standalone_question": {"type": "string"}},
    "required": ["standalone_question"],
}


async def rewrite_followup(
    question: str, history: List[Dict[str, str]]
) -> Optional[str]:
    """Resolve a conversation-dependent follow-up into a standalone question
    for retrieval. Returns None when the rewrite fails or looks degenerate —
    callers fall back to prior-turn concatenation."""
    lines = []
    for turn in history[-6:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = (turn.get("content") or "").strip().replace("\n", " ")
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"{role}: {text}")
    lines.append(f"User (follow-up to rewrite): {question}")
    try:
        out = await llm.structured_call(REWRITE_SYSTEM, "\n".join(lines), REWRITE_SCHEMA)
    except Exception:
        return None
    rewritten = (out.get("standalone_question") or "").strip()
    if not rewritten or len(rewritten) > 500:
        return None
    return rewritten

QUERY_SYSTEM = """You are a company's institutional memory — its "second brain". You answer \
questions using ONLY the memory provided: source chunks (primary evidence) and the memory \
graph (distilled decisions, entities, questions and their typed relationships). \
Today's date is {today}.

How to reason:
- Identify which decisions in memory are relevant to the question.
- Reconstruct the context at the time each decision was made: the reasoning, who advocated \
what, and what alternatives were on the table.
- Check whether anything was revisited, reversed, or reaffirmed later — use graph edges \
(revisits, resolves, relates_to) and document dates to build the chronology.
- Attribute positions to specific people when the sources support it.
- Note related decisions and open questions that bear on the answer.
- Surface counter-evidence: dissenting opinions, caveats, risks, unresolved \
concerns, or later developments that complicate or push back on the main answer. \
Pull these from the actual memory — if there is genuinely none, say so; never \
manufacture disagreement.
- Be explicit about what memory does NOT contain. Never invent facts, people, or dates.

Answer format:
- Markdown. Cite every factual claim inline with the chunk marker, e.g. [C12]. Cite \
generously — every paragraph should carry citations.
- Mention dates so the reader can follow the chronology.
- If memory cannot answer the question, say so plainly and point to the nearest related \
memory instead.

After the answer, output a line containing exactly:
{delim}
followed by a single JSON object (no prose after it):
{{"confidence": "high" | "medium" | "low",
  "cited_chunk_ids": [<int chunk ids you actually cited>],
  "related_questions": ["<2-4 follow-up questions this memory could answer>"],
  "timeline": [{{"date": "YYYY-MM-DD", "event": "<short event description>"}}],
  "counter_evidence": [{{"point": "<a caveat, dissent, risk, or contradicting fact \
from memory>", "chunk_ids": [<int chunk ids backing this point>]}}]}}"""


def _node_line(n: Dict[str, Any]) -> str:
    parts = [f"- [N{n['id']}] {n['kind']}: \"{n['label']}\""]
    if n.get("status"):
        parts.append(f"(status: {n['status']})")
    data = n.get("data") or {}
    if data.get("made_by"):
        parts.append(f"| people: {', '.join(data['made_by'])}")
    if data.get("date"):
        parts.append(f"| date: {data['date']}")
    line = " ".join(parts)
    summary = (n.get("summary") or "").replace("\n", " ")
    if summary:
        if len(summary) > 400:
            summary = summary[:400] + "…"
        line += f"\n  {summary}"
    if data.get("positions"):
        line += f"\n  positions: {'; '.join(data['positions'])}"
    if data.get("alternatives_considered"):
        line += f"\n  alternatives considered: {', '.join(data['alternatives_considered'])}"
    if data.get("resolution"):
        line += f"\n  resolution: {data['resolution']}"
    return line


def build_context(
    question: str, ret: Dict[str, Any], history: Optional[List[Dict[str, str]]] = None
) -> str:
    lines = []
    if history:
        lines += ["# CONVERSATION SO FAR (for follow-up context)"]
        for turn in history[-6:]:
            role = "User" if turn.get("role") == "user" else "You"
            text = (turn.get("content") or "").strip().replace("\n", " ")
            if len(text) > 600:
                text = text[:600] + "…"
            lines.append(f"{role}: {text}")
        lines.append("")
    lines += ["# QUESTION", question, ""]
    lines.append("# MEMORY GRAPH (distilled memory)")
    if ret["nodes"]:
        labels = {n["id"]: n["label"] for n in ret["nodes"]}
        lines.append("Nodes:")
        for n in ret["nodes"]:
            lines.append(_node_line(n))
        if ret["edges"]:
            lines.append("")
            lines.append("Edges:")
            for e in ret["edges"]:
                src = labels.get(e["src"], f"N{e['src']}")
                dst = labels.get(e["dst"], f"N{e['dst']}")
                lines.append(f"- [N{e['src']}] \"{src}\" --{e['relation']}--> [N{e['dst']}] \"{dst}\"")
    else:
        lines.append("(no graph memory matched)")
    lines += ["", "# SOURCE CHUNKS (primary evidence, chronological — cite as [C<id>])"]
    for c in ret["chunks"]:
        lines.append(
            f"[C{c['id']}] {c['source']} — \"{c['title']}\" — "
            f"{c['author'] or 'unknown author'} — {c['date'] or 'undated'}"
        )
        lines.append(c["text"])
        lines.append("")
    return "\n".join(lines)


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


async def stream_query(
    question: str, workspace_id: int, history: Optional[List[Dict[str, str]]] = None
) -> AsyncIterator[str]:
    timer = StageTimer()
    # Short follow-ups ("was it ever reversed?") retrieve poorly verbatim.
    # First choice: LLM-rewrite into a standalone question (resolves pronouns
    # for both vector and full-text search). Fallback when the rewrite fails:
    # fold in the previous user turn so vector search has something to bite on.
    search_q = question
    embed_text = question
    rewritten = None
    if history and len(question) < FOLLOWUP_MAX_CHARS:
        yield _sse("status", {"stage": "rephrasing",
                              "message": "Rephrasing follow-up for memory search…"})
        rewritten = await rewrite_followup(question, history)
        if rewritten:
            search_q = rewritten
            embed_text = rewritten
        else:
            prior = [t.get("content", "") for t in history if t.get("role") == "user"]
            if prior:
                embed_text = f"{prior[-1]}\n{question}"
        timer.lap("rewrite")
    yield _sse("status", {"stage": "retrieving", "message": "Searching memory…"})
    ret = await retrieval.retrieve(search_q, workspace_id=workspace_id, embed_text=embed_text)
    timer.lap("retrieval")
    if rewritten:
        ret["trace"]["rewritten_question"] = rewritten
    sources = []
    seen = set()
    for c in ret["chunks"]:
        key = (c["source"], c["title"])
        if key not in seen:
            seen.add(key)
            sources.append({"source": c["source"], "title": c["title"], "date": c["date"]})
    yield _sse("status", {
        "stage": "reasoning",
        "message": f"Reasoning over {len(ret['chunks'])} chunks and "
                   f"{len(ret['nodes'])} memory nodes…",
        "chunks": len(ret["chunks"]),
        "nodes": len(ret["nodes"]),
        "edges": len(ret["edges"]),
        "sources": sources[:8],
    })

    system = QUERY_SYSTEM.format(today=date.today().isoformat(), delim=DELIM)
    context = build_context(question, ret, history=history)
    timer.lap("context")

    answer_text = ""
    buf = ""
    meta_raw = ""
    in_meta = False
    holdback = len(DELIM) + 2  # never emit a partial delimiter as answer text

    try:
        async with llm.stream_text(system, context) as stream:
            async for piece in stream.text_stream:
                if in_meta:
                    meta_raw += piece
                    continue
                buf += piece
                idx = buf.find(DELIM)
                if idx != -1:
                    head = buf[:idx].rstrip()
                    if head:
                        answer_text += head
                        yield _sse("delta", {"text": head})
                    meta_raw = buf[idx + len(DELIM):]
                    buf = ""
                    in_meta = True
                elif len(buf) > holdback:
                    emit = buf[:-holdback]
                    buf = buf[-holdback:]
                    answer_text += emit
                    yield _sse("delta", {"text": emit})
        timer.lap("llm")
    except Exception as e:
        timer.lap("llm_error")
        log.exception("query failed workspace=%s timings %s", workspace_id, timer.line())
        yield _sse("error", {"message": f"Claude call failed: {e}"})
        return

    if not in_meta and buf:
        answer_text += buf
        yield _sse("delta", {"text": buf})

    meta = llm.parse_loose_json(meta_raw)
    cited_ids = {int(i) for i in meta.get("cited_chunk_ids", []) if str(i).isdigit()}
    cited_ids |= {int(m) for m in _CITE_RE.findall(answer_text)}
    by_id = {c["id"]: c for c in ret["chunks"]}
    citations = []
    for cid in sorted(cited_ids):
        c = by_id.get(cid)
        if not c:
            continue
        snippet = c["text"][:240] + ("…" if len(c["text"]) > 240 else "")
        citations.append({
            "chunk_id": cid,
            "document_id": c["document_id"],
            "source": c["source"],
            "title": c["title"],
            "author": c["author"],
            "date": c["date"],
            "snippet": snippet,
            "text": c["text"],  # full chunk so the UI can highlight it in-document
        })

    counter_evidence = []
    for item in meta.get("counter_evidence", []) or []:
        point = (item.get("point") or "").strip() if isinstance(item, dict) else ""
        if not point:
            continue
        ev = []
        for cid in (item.get("chunk_ids", []) if isinstance(item, dict) else []):
            if not str(cid).isdigit():
                continue
            c = by_id.get(int(cid))
            if c:
                ev.append({"chunk_id": int(cid), "document_id": c["document_id"],
                           "source": c["source"], "title": c["title"], "date": c["date"]})
        counter_evidence.append({"point": point, "evidence": ev})

    yield _sse("metadata", {
        "confidence": meta.get("confidence", "unknown"),
        "citations": citations,
        "related_questions": meta.get("related_questions", []),
        "timeline": meta.get("timeline", []),
        "counter_evidence": counter_evidence,
        "trace": ret.get("trace", {}),
    })
    timer.lap("metadata")
    log.info(
        "query complete workspace=%s chunks=%d nodes=%d edges=%d timings %s",
        workspace_id, len(ret["chunks"]), len(ret["nodes"]), len(ret["edges"]),
        timer.line(),
    )
    yield _sse("done", {})
