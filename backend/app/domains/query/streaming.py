"""Query engine: graph-aware retrieval + LLM reasoning, streamed as SSE.

The LLM streams a markdown answer with [C<id>] citations, then a metadata block
(confidence, cited chunk ids, related questions, timeline) after a delimiter.
The answer streams to the client token by token; the metadata is buffered,
parsed, enriched with provenance, and emitted as a final event."""

import json
import logging
import re
from datetime import date
from typing import Any, AsyncIterator, Dict, List, Optional

from app.providers import llm
from app.core import config, usage
from app.core.observability import StageTimer
from . import retrieval

DELIM = "<<<MEMORY_METADATA>>>"
_CITE_RE = re.compile(r"\[C(\d+)\]")
_INSIGHT_CARD_TYPES = {
    "why_it_won", "tradeoffs", "alternatives", "open_questions", "decision_anatomy",
}
_METADATA_BLEED_PATTERNS = [
    re.compile(
        r"(?ims)\n\s*(?:#{1,6}\s*)?"
        r"(?:timeline|chronology|counter[-\s]?evidence|pushback\s*&\s*counter[-\s]?evidence|"
        r"caveats?|worth asking next|memory could answer|related questions|follow[-\s]?up questions)"
        r"\s*:?\s*\n.*$"
    ),
    re.compile(
        r"(?ims)\n\s*\|\s*[^|\n]*(?:date|event|timeline|caveat|counter[-\s]?evidence|"
        r"supporting chunk|supporting source)[^|\n]*\|.*$"
    ),
    re.compile(r"(?ims)\n\s*confidence\s*:\s*(?:high|medium|low|unknown)\b.*$"),
]
log = logging.getLogger("ybase.query")


def _tail_holdback_chars() -> int:
    """How much answer tail to buffer before streaming it to the client.

    The buffer lets card-shaped metadata leaks (_METADATA_BLEED_PATTERNS) be
    stripped before the frontend sees them, and keeps the delimiter from ever
    streaming partially — but every buffered char delays the first visible
    token, and an answer shorter than the buffer doesn't stream at all. Claude
    follows the no-cards instruction reliably, so it gets a small buffer;
    the bleed patterns exist for smaller models, which keep a larger one."""
    return 400 if llm.active_provider() == "anthropic" else 1500


def _format_history(history: List[Dict[str, str]], max_chars: int,
                    assistant_label: str = "Assistant") -> List[str]:
    """Last 6 turns as 'Role: text' lines, one per turn, truncated per turn."""
    lines = []
    for turn in history[-6:]:
        role = "User" if turn.get("role") == "user" else assistant_label
        text = (turn.get("content") or "").strip().replace("\n", " ")
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        lines.append(f"{role}: {text}")
    return lines


def locate_quote(chunk_text: str, quote: Optional[str]) -> Optional[str]:
    """Return the exact substring of `chunk_text` to highlight for a citation,
    or None if the model's quote can't be located. Tries an exact match, then a
    whitespace-tolerant match (models often re-flow spacing/newlines when copying),
    mapping back to the original span so the result is verbatim from the chunk —
    and therefore highlightable inside the source document."""
    if not chunk_text or not quote:
        return None
    q = quote.strip()
    if not q:
        return None
    if q in chunk_text:
        return q
    tokens = [re.escape(t) for t in q.split()]
    if not tokens:
        return None
    m = re.search(r"\s+".join(tokens), chunk_text)
    return m.group(0) if m else None


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
    lines = _format_history(history, max_chars=400)
    lines.append(f"User (follow-up to rewrite): {question}")
    try:
        out = await llm.structured_call(REWRITE_SYSTEM, "\n".join(lines), REWRITE_SCHEMA,
                                        max_tokens=300, effort="low")
    except Exception:
        log.warning("follow-up rewrite failed; falling back to prior-turn concat",
                    exc_info=True)
        return None
    rewritten = (out.get("standalone_question") or "").strip()
    if not rewritten or len(rewritten) > 500:
        return None
    return rewritten

QUERY_SYSTEM = """You are a company's institutional memory — its "YBase". You answer \
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
- Start with a short, direct takeaway sentence. Make it feel like a smart teammate \
summarizing the answer, not a report header.
- Markdown. Cite every factual claim inline with the chunk marker, e.g. [C12]. Cite \
generously — every paragraph should carry citations.
- Mention dates so the reader can follow the chronology.
- If memory cannot answer the question, say so plainly and point to the nearest related \
memory instead.
- The visible markdown answer is for the direct narrative answer only: paragraphs and \
short bullets are allowed, but markdown tables are not.
- Do NOT include tables or separate sections for timeline, chronology, caveats, pushback, \
counter-evidence, confidence, sources, citations, related questions, follow-up questions, \
"you might ask", "memory could answer", or "worth asking next" in the markdown answer.
- The UI renders separate cards from the JSON metadata: confidence badge, timeline card, \
pushback/counter-evidence card, insight cards, source citations, trace, and related-question \
chips. Put that card content only in the JSON metadata after the delimiter.
- Do NOT add a horizontal rule, metadata heading, or summary section before the delimiter.

After the answer, output a line containing exactly:
{delim}
followed by a single JSON object (no prose after it):
{{"takeaway": "<same short takeaway, without citations>",
  "confidence": "high" | "medium" | "low",
  "citations": [{{"chunk_id": <int chunk id you cited>, "quote": "<the exact words copied verbatim from that chunk that back the claim — the shortest 1-2 sentence span, character-for-character, no paraphrasing and no ellipsis>"}}],
  "related_questions": ["<2-4 follow-up questions this memory could answer>"],
  "timeline": [{{"date": "YYYY-MM-DD", "event": "<short event description>"}}],
  "counter_evidence": [{{"point": "<a caveat, dissent, risk, or contradicting fact \
from memory>", "chunk_ids": [<int chunk ids backing this point>]}}],
  "insight_cards": [
    {{"type": "why_it_won" | "tradeoffs" | "alternatives" | "open_questions" | \
"decision_anatomy",
      "title": "<short card title>",
      "items": [
        {{"label": "<short label>",
          "detail": "<one sentence explanation>",
          "chunk_ids": [<int chunk ids backing this item>]}}
      ]}}
  ]}}"""


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
        lines += _format_history(history, max_chars=600, assistant_label="You")
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


def _strip_metadata_bleed(text: str) -> str:
    """Remove card-shaped metadata sections that smaller models sometimes put
    in the visible answer just before the metadata delimiter."""
    cut = len(text)
    for pattern in _METADATA_BLEED_PATTERNS:
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    cleaned = text[:cut].rstrip()
    cleaned = re.sub(r"(?m)(?:\n\s*-{3,}\s*)+\Z", "", cleaned).rstrip()
    return cleaned


def _source_refs(chunk_ids: List[Any], by_id: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    refs = []
    seen = set()
    for cid in chunk_ids or []:
        if not str(cid).isdigit():
            continue
        chunk_id = int(cid)
        if chunk_id in seen:
            continue
        c = by_id.get(chunk_id)
        if c:
            refs.append({
                "chunk_id": chunk_id,
                "document_id": c["document_id"],
                "source": c["source"],
                "title": c["title"],
                "date": c["date"],
            })
            seen.add(chunk_id)
    return refs


def _clean_timeline(raw: Any) -> List[Dict[str, str]]:
    """Keep only well-shaped {date, event} entries so a malformed metadata
    block can't push junk items into the frontend's timeline card."""
    if not isinstance(raw, list):
        return []
    events = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        date_s, event_s = item.get("date"), item.get("event")
        if isinstance(date_s, str) and isinstance(event_s, str) and event_s.strip():
            events.append({"date": date_s, "event": event_s.strip()})
    return events


def _clean_related_questions(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [q.strip() for q in raw if isinstance(q, str) and q.strip()][:6]


def _enrich_insight_cards(
    raw_cards: Any, by_id: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not isinstance(raw_cards, list):
        return []
    cards = []
    for card in raw_cards[:5]:
        if not isinstance(card, dict):
            continue
        card_type = card.get("type") if card.get("type") in _INSIGHT_CARD_TYPES else "decision_anatomy"
        title = (card.get("title") or "").strip()
        items = []
        for item in (card.get("items") or [])[:6]:
            if not isinstance(item, dict):
                continue
            label = (item.get("label") or "").strip()
            detail = (item.get("detail") or "").strip()
            if not label and not detail:
                continue
            items.append({
                "label": label,
                "detail": detail,
                "sources": _source_refs(item.get("chunk_ids", []), by_id),
            })
        if title and items:
            cards.append({"type": card_type, "title": title, "items": items})
    return cards


async def stream_query(
    question: str, workspace_id: int, history: Optional[List[Dict[str, str]]] = None
) -> AsyncIterator[str]:
    timer = StageTimer()
    # Attribute this request's LLM/embedding calls (rewrite, retrieval query
    # embed, answer stream). Task-scoped — each request runs in its own task,
    # so no reset needed (same pattern as the request-id contextvar).
    usage.set_context(workspace_id=workspace_id, surface="query")
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
    try:
        ret = await retrieval.retrieve(search_q, workspace_id=workspace_id, embed_text=embed_text)
    except Exception as e:
        # An embeddings/DB failure here would otherwise kill the SSE stream
        # with no event — the frontend would just see a dropped connection.
        timer.lap("retrieval_error")
        log.exception("retrieval failed workspace=%s timings %s", workspace_id, timer.line())
        yield _sse("error", {"message": f"Memory search failed: {e}"})
        return
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
    # Keep enough tail text to remove card-shaped metadata leaks before the
    # frontend sees them. The delimiter itself must also never stream partially.
    holdback = max(len(DELIM) + 2, _tail_holdback_chars())

    try:
        async with llm.stream_text(system, context) as stream:
            async for piece in stream.text_stream:
                if in_meta:
                    meta_raw += piece
                    continue
                buf += piece
                idx = buf.find(DELIM)
                if idx != -1:
                    head = _strip_metadata_bleed(buf[:idx]).rstrip()
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
            if hasattr(stream, "get_final_message"):
                # Anthropic reports usage on the final message; the stream is
                # exhausted so this resolves immediately. Best-effort only.
                try:
                    final = await stream.get_final_message()
                    await usage.record("llm", "anthropic", config.ANTHROPIC_MODEL,
                                       **usage.usage_from_anthropic(final))
                except Exception:
                    log.debug("stream usage capture failed", exc_info=True)
        timer.lap("llm")
    except Exception as e:
        timer.lap("llm_error")
        log.exception("query failed workspace=%s timings %s", workspace_id, timer.line())
        yield _sse("error", {"message": f"LLM call failed: {e}"})
        return

    if not in_meta and buf:
        head = _strip_metadata_bleed(buf)
        if head:
            answer_text += head
            yield _sse("delta", {"text": head})

    meta = llm.parse_loose_json(meta_raw)
    # Preferred shape: citations:[{chunk_id, quote}]. Fall back to the old flat
    # cited_chunk_ids and inline [C<id>] markers so looser/older model outputs
    # still yield citations — just without a precise supporting quote.
    quote_by_id: Dict[int, str] = {}
    cited_ids = set()
    for item in meta.get("citations", []) or []:
        if not isinstance(item, dict):
            continue
        cid = item.get("chunk_id")
        if not (isinstance(cid, int) or (isinstance(cid, str) and cid.isdigit())):
            continue
        cid = int(cid)
        cited_ids.add(cid)
        q = item.get("quote")
        if isinstance(q, str) and q.strip():
            quote_by_id[cid] = q
    cited_ids |= {int(i) for i in meta.get("cited_chunk_ids", []) if str(i).isdigit()}
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
            "quote": locate_quote(c["text"], quote_by_id.get(cid)),  # precise span, or None
            "text": c["text"],  # full chunk so the UI can fall back to highlighting it
        })

    counter_evidence = []
    for item in meta.get("counter_evidence", []) or []:
        point = (item.get("point") or "").strip() if isinstance(item, dict) else ""
        if not point:
            continue
        ev = _source_refs(item.get("chunk_ids", []) if isinstance(item, dict) else [], by_id)
        counter_evidence.append({"point": point, "evidence": ev})

    yield _sse("metadata", {
        "takeaway": meta.get("takeaway"),
        "confidence": meta.get("confidence", "unknown"),
        "citations": citations,
        "related_questions": _clean_related_questions(meta.get("related_questions")),
        "timeline": _clean_timeline(meta.get("timeline")),
        "counter_evidence": counter_evidence,
        "insight_cards": _enrich_insight_cards(meta.get("insight_cards", []), by_id),
        "trace": ret.get("trace", {}),
    })
    timer.lap("metadata")
    log.info(
        "query complete workspace=%s chunks=%d nodes=%d edges=%d timings %s",
        workspace_id, len(ret["chunks"]), len(ret["nodes"]), len(ret["edges"]),
        timer.line(),
    )
    yield _sse("done", {})
