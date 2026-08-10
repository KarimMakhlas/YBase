"""Memory formation — the async step that runs after every ingest.

An LLM reads the new document plus a digest of the existing memory graph and
extracts decisions (with reasoning, advocates, alternatives, status), entities,
and open/resolved questions — each tied to evidence chunks and linked to
existing memory nodes (revisits / resolves / relates_to).
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import asyncpg

from app.core import config, db
from app.core.observability import StageTimer
from app.providers import llm
from . import graph, observations, validation

log = logging.getLogger("ybase.formation")


@dataclass
class FormationOutcome:
    touched: List[int] = field(default_factory=list)  # decision node ids
    validation: Dict[str, Any] = field(default_factory=dict)

_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}
_INT_ARRAY = {"type": "array", "items": {"type": "integer"}}

FORMATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "context_summary": {
            "type": "string",
            "description": "2-3 sentences: what this document contributes to company memory.",
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "description": "Short canonical name, e.g. 'Use PostgreSQL as the primary database'. Reuse the exact existing label when this is the same decision."},
                    "what": {"type": "string", "description": "What was decided or proposed."},
                    "reasoning": {"type": "string", "description": "WHY — the rationale at the time, as close to the source's own words as possible."},
                    "status": {"type": "string", "enum": ["decided", "proposed", "revisited", "reversed", "reaffirmed"]},
                    "made_by": {**_STR_ARRAY, "description": "People who advocated for or made this decision."},
                    "positions": {**_STR_ARRAY, "description": "Per-person stances, e.g. 'Maya Chen: argued for Postgres (transactions)'."},
                    "alternatives_considered": _STR_ARRAY,
                    "topics": {**_STR_ARRAY, "description": "1-3 short topic tags, e.g. 'database', 'caching'."},
                    "date": {**_NULLABLE_STR, "description": "ISO date of the decision if stated, else null."},
                    "evidence_chunk_indexes": _INT_ARRAY,
                    "revisits_node_id": {**_NULLABLE_INT, "description": "Existing decision node id that this revisits/reverses/reaffirms, else null."},
                    "resolves_question_node_id": {**_NULLABLE_INT, "description": "Existing question node id that this decision answers, else null."},
                    "relates_to_node_ids": {**_INT_ARRAY, "description": "Other existing node ids this connects to."},
                },
                "required": [
                    "title", "what", "reasoning", "status", "made_by", "positions",
                    "alternatives_considered", "topics", "date",
                    "evidence_chunk_indexes", "revisits_node_id",
                    "resolves_question_node_id", "relates_to_node_ids",
                ],
            },
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "description": "Canonical name; reuse the existing spelling if already in memory."},
                    "kind": {"type": "string", "enum": ["person", "project", "system", "feature", "team", "other"]},
                    "description": {"type": "string"},
                    "evidence_chunk_indexes": _INT_ARRAY,
                },
                "required": ["name", "kind", "description", "evidence_chunk_indexes"],
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "resolved"]},
                    "resolution": _NULLABLE_STR,
                    "raised_by": _STR_ARRAY,
                    "topics": _STR_ARRAY,
                    "evidence_chunk_indexes": _INT_ARRAY,
                    "resolves_node_id": {**_NULLABLE_INT, "description": "Existing question node id that this content resolves, else null."},
                    "relates_to_node_ids": {**_INT_ARRAY, "description": "Existing node ids this question bears on — especially decisions it contradicts or challenges."},
                },
                "required": [
                    "question", "status", "resolution", "raised_by", "topics",
                    "evidence_chunk_indexes", "resolves_node_id", "relates_to_node_ids",
                ],
            },
        },
    },
    "required": ["context_summary", "decisions", "entities", "questions"],
}

FORMATION_SYSTEM = """You are the memory-formation engine of a company's "YBase" — an \
institutional memory layer over Slack, Notion, GitHub, Jira and meeting notes.

Given a newly ingested document (split into indexed chunks) and a digest of the EXISTING \
memory graph, extract durable organizational memory:

1. DECISIONS — choices the team made, proposed, revisited, reversed, or reaffirmed.
   - Capture the REASONING at the time (this is the most valuable part — preserve the "why",
     close to the source's wording). Record who advocated, each person's position, and the
     alternatives that were considered.
   - If the document revisits / reverses / reaffirms a decision already in memory, set
     revisits_node_id to that node's id AND reuse its exact title so it merges rather than
     duplicates. Set status to revisited/reversed/reaffirmed accordingly.
   - If a decision answers an open question already in memory, set resolves_question_node_id.

2. ENTITIES — people, projects, systems, features, teams that play a meaningful role.
   Reuse existing names exactly when the same real-world thing is meant.

3. QUESTIONS — open questions raised here, and whether this document resolves an existing
   open question (set resolves_node_id and status "resolved" with the resolution).

4. CONFLICTS — if this document contradicts or undermines an existing decision WITHOUT
   explicitly deciding anything new (new evidence, complaints, data that challenges the old
   reasoning), record an open question describing the tension and set relates_to_node_ids
   to the challenged decision so the conflict is visible in the graph.

Rules:
- evidence_chunk_indexes must only use the chunk indexes shown in the document.
- Do not invent decisions. Routine chatter, status updates and small talk are not memory.
- Prefer FEW high-quality memories over many trivial ones (typically 0-3 decisions per doc).
- Topics are short lowercase tags ("database", "caching", "scaling") — reuse existing ones.
  EVERY decision must carry at least one topic.
- context_summary: 2-3 sentences on what this document adds to company memory."""


_TOPIC_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "use", "using",
    "move", "moving", "choose", "switch", "make", "new", "our", "all", "over",
    "about", "decision", "proposal", "plan", "team", "company",
}


def fallback_topics(title: str, doc_tags: List[str]) -> List[str]:
    """A decision must never be topicless — the graph goes edgeless and the
    whole memory-not-search retrieval story dies (local models routinely
    return empty topics). Prefer the document's own tags, else mine the
    decision title for content words."""
    tags = [t.strip().lower() for t in doc_tags if t.strip()]
    if tags:
        return tags[:2]
    words = [w.strip(".,:;()[]\"'").lower() for w in title.split()]
    candidates = [w for w in words if len(w) > 3 and w not in _TOPIC_STOPWORDS
                  and not w.isdigit()]
    candidates.sort(key=len, reverse=True)
    return candidates[:2] or ["general"]


def _format_existing(rows: List[asyncpg.Record]) -> str:
    if not rows:
        return "(memory is empty — this is the first document)"
    by_kind: Dict[str, List[str]] = {}
    for r in rows:
        summary = (r["summary"] or "").replace("\n", " ")
        if len(summary) > 220:
            summary = summary[:220] + "…"
        line = f"- id={r['id']} \"{r['label']}\""
        if r["status"]:
            line += f" (status: {r['status']})"
        if summary:
            line += f" — {summary}"
        by_kind.setdefault(r["kind"], []).append(line)
    sections = []
    for kind in ("decision", "question", "entity", "topic"):
        if kind in by_kind:
            sections.append(f"{kind.upper()}S:\n" + "\n".join(by_kind[kind]))
    return "\n\n".join(sections)


def _build_user_prompt(doc: asyncpg.Record, chunks: List[asyncpg.Record],
                       existing: List[asyncpg.Record]) -> str:
    date = doc["doc_created_at"].date().isoformat() if doc["doc_created_at"] else "unknown"
    lines = [
        "# NEW DOCUMENT",
        f"source: {doc['source']}",
        f"title: {doc['title']}",
        f"author: {doc['author'] or 'unknown'}",
        f"date: {date}",
        f"tags: {', '.join(doc['tags']) if doc['tags'] else '(none)'}",
        "",
        "## Chunks",
    ]
    for c in chunks:
        lines.append(f"[chunk {c['chunk_index']}]")
        lines.append(c["text"])
        lines.append("")
    lines.append("# EXISTING MEMORY GRAPH (ids usable in revisits_node_id / resolves_node_id / "
                 "resolves_question_node_id / relates_to_node_ids)")
    lines.append(_format_existing(existing))
    return "\n".join(lines)


async def _persist(
    conn: asyncpg.Connection,
    workspace_id: int,
    document_id: int,
    chunks: List[asyncpg.Record],
    result: Dict[str, Any],
    valid_node_ids: Set[int],
    doc_tags: Optional[List[str]] = None,
) -> List[int]:
    """Write the extraction into the graph. Returns the decision node ids this
    document created or updated, for incremental consolidation."""
    from app.domains.auth import service as auth  # lazy: avoid import cycle

    index_to_id = {c["chunk_index"]: c["id"] for c in chunks}
    doc_chunk_ids = list(index_to_id.values())

    def chunk_ids_for(indexes: List[int]) -> List[int]:
        ids = [index_to_id[i] for i in indexes if i in index_to_id]
        return ids or doc_chunk_ids[:1]  # always keep provenance to the doc

    def safe_node(node_id: Optional[int]) -> Optional[int]:
        return node_id if node_id in valid_node_ids else None

    async def flip_status(node_id: int, new_status: str) -> None:
        """Status changes on *existing* nodes (reversals, resolutions) are the
        destructive edge of formation — leave an audit trail."""
        old = await graph.set_status(conn, node_id, new_status)
        if old is not None and old != new_status:
            await auth.audit(
                conn, "formation_node_status_change", workspace_id, None,
                target_type="memory_node", target_id=node_id,
                data={"old_status": old, "new_status": new_status,
                      "document_id": document_id},
            )

    entity_ids: Dict[str, int] = {}
    touched_decisions: List[int] = []

    async def ensure_entity(name: str, kind: str = "person", description: str = "") -> int:
        key = name.strip().lower()
        if key not in entity_ids:
            entity_ids[key] = await graph.upsert_node(
                conn, workspace_id, "entity", name, summary=description or None,
                data={"entity_kind": kind},
            )
        return entity_ids[key]

    async def ensure_topic(name: str) -> int:
        return await graph.upsert_node(conn, workspace_id, "topic", name.strip().lower())

    for ent in result.get("entities", []):
        node_id = await ensure_entity(ent["name"], ent["kind"], ent.get("description", ""))
        for cid in chunk_ids_for(ent.get("evidence_chunk_indexes", [])):
            await graph.link_chunk(conn, cid, node_id)

    for dec in result.get("decisions", []):
        summary = dec["what"].strip()
        if dec.get("reasoning"):
            summary += "\n\nReasoning: " + dec["reasoning"].strip()
        node_id = await graph.upsert_node(
            conn, workspace_id, "decision", dec["title"], summary=summary, status=dec["status"],
            data={
                "made_by": dec.get("made_by", []),
                "positions": dec.get("positions", []),
                "alternatives_considered": dec.get("alternatives_considered", []),
                "date": dec.get("date"),
            },
        )
        for cid in chunk_ids_for(dec.get("evidence_chunk_indexes", [])):
            await graph.link_chunk(conn, cid, node_id)
        for person in dec.get("made_by", []):
            pid = await ensure_entity(person)
            await graph.add_edge(conn, workspace_id, node_id, pid, "involves")
        topics = [t for t in dec.get("topics", []) if t.strip()]
        if not topics:
            topics = fallback_topics(dec["title"], doc_tags or [])
        for topic in topics:
            tid = await ensure_topic(topic)
            await graph.add_edge(conn, workspace_id, node_id, tid, "about")
        revisits = safe_node(dec.get("revisits_node_id"))
        if revisits and revisits != node_id:
            await graph.add_edge(conn, workspace_id, node_id, revisits, "revisits")
            if dec["status"] == "reversed":
                await flip_status(revisits, "reversed")
            elif dec["status"] == "revisited":
                await flip_status(revisits, "revisited")
            await graph.merge_data(conn, revisits, {"last_revisited": dec.get("date")})
        resolves_q = safe_node(dec.get("resolves_question_node_id"))
        if resolves_q:
            await graph.add_edge(conn, workspace_id, node_id, resolves_q, "resolves")
            await flip_status(resolves_q, "resolved")
            await graph.merge_data(conn, resolves_q, {"resolution": dec["what"]})
        for rel in dec.get("relates_to_node_ids", []):
            rel_id = safe_node(rel)
            if rel_id and rel_id != node_id:
                await graph.add_edge(conn, workspace_id, node_id, rel_id, "relates_to")
        valid_node_ids.add(node_id)
        touched_decisions.append(node_id)

    for q in result.get("questions", []):
        resolves = safe_node(q.get("resolves_node_id"))
        if resolves:
            node_id = resolves
            await flip_status(node_id, "resolved")
            await graph.merge_data(conn, node_id, {"resolution": q.get("resolution")})
        else:
            node_id = await graph.upsert_node(
                conn, workspace_id, "question", q["question"], status=q["status"],
                data={"resolution": q.get("resolution"), "raised_by": q.get("raised_by", [])},
            )
        for cid in chunk_ids_for(q.get("evidence_chunk_indexes", [])):
            await graph.link_chunk(conn, cid, node_id)
        for person in q.get("raised_by", []):
            pid = await ensure_entity(person)
            await graph.add_edge(conn, workspace_id, node_id, pid, "raised_by")
        for topic in q.get("topics", []):
            tid = await ensure_topic(topic)
            await graph.add_edge(conn, workspace_id, node_id, tid, "about")
        for rel in q.get("relates_to_node_ids", []):
            rel_id = safe_node(rel)
            if rel_id and rel_id != node_id:
                await graph.add_edge(conn, workspace_id, node_id, rel_id, "relates_to")
        valid_node_ids.add(node_id)

    await conn.execute(
        "UPDATE documents SET formation_status='complete', context_summary=$2, "
        "formation_error=NULL, formation_attempts=0 WHERE id=$1",
        document_id, result.get("context_summary", ""),
    )
    return touched_decisions


_EXISTING_CAP = 150       # total nodes shown to the extraction LLM
_EXISTING_RECENT = 50     # slots reserved for the most recently updated nodes
_EXISTING_RELEVANT = 100  # slots for nodes similar to the new document
_EXISTING_TOPIC = 30      # slots for same-topic decisions/questions


async def _fetch_existing(
    conn: asyncpg.Connection,
    workspace_id: int,
    document_id: int,
    doc_tags: Optional[List[str]] = None,
) -> List[asyncpg.Record]:
    """Pick the existing-memory digest the extraction LLM sees.

    Recency alone starves large workspaces: an old decision this document
    revisits may not be among the last 150 updated nodes, so the LLM can't
    link to it and forms a duplicate instead. Blend three slices, deduped
    into the cap:
      1. recent nodes (carry the entity/topic labels to reuse),
      2. decisions/questions embedding-similar to the document (stored
         signature vectors — NULL until consolidation first touches a node,
         so a fresh database falls back to recency),
      3. topic neighbors — decisions/questions sharing a topic with the
         document's tags or with the top similar nodes. This catches the old
         same-topic decision that neither recency nor wording similarity
         surfaces (the classic unlinked-revisit failure)."""
    recent = await conn.fetch(
        "SELECT id, kind, label, summary, status FROM memory_nodes "
        "WHERE workspace_id=$1 AND kind IN ('decision', 'question', 'entity', 'topic') "
        "AND archived_at IS NULL "
        "ORDER BY updated_at DESC LIMIT $2",
        workspace_id, _EXISTING_CAP,
    )
    centroid = await conn.fetchval(
        "SELECT avg(embedding)::text FROM chunks WHERE document_id=$1", document_id
    )
    relevant: List[asyncpg.Record] = []
    if centroid:
        relevant = await conn.fetch(
            "SELECT id, kind, label, summary, status FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind IN ('decision', 'question') "
            "AND archived_at IS NULL AND embedding IS NOT NULL "
            "ORDER BY embedding <=> $2::vector LIMIT $3",
            workspace_id, centroid, _EXISTING_RELEVANT,
        )

    topic_ids: Set[int] = set()
    tags = [t.strip().lower() for t in (doc_tags or []) if t.strip()]
    if tags:
        rows = await conn.fetch(
            "SELECT id FROM memory_nodes WHERE workspace_id=$1 AND kind='topic' "
            "AND lower(label) = ANY($2::text[]) AND archived_at IS NULL",
            workspace_id, tags,
        )
        topic_ids.update(r["id"] for r in rows)
    top_relevant = [r["id"] for r in relevant[:10]]
    if top_relevant:
        rows = await conn.fetch(
            "SELECT DISTINCT e.dst AS id FROM memory_edges e "
            "JOIN memory_nodes t ON t.id = e.dst "
            "WHERE e.workspace_id=$1 AND e.relation='about' "
            "AND e.src = ANY($2::int[]) AND t.kind='topic' AND t.archived_at IS NULL",
            workspace_id, top_relevant,
        )
        topic_ids.update(r["id"] for r in rows)
    topic_neighbors: List[asyncpg.Record] = []
    if topic_ids:
        topic_neighbors = await conn.fetch(
            "SELECT DISTINCT n.id, n.kind, n.label, n.summary, n.status, n.updated_at "
            "FROM memory_nodes n "
            "JOIN memory_edges e ON e.src = n.id AND e.relation='about' "
            "WHERE n.workspace_id=$1 AND e.dst = ANY($2::int[]) "
            "AND n.kind IN ('decision', 'question') AND n.archived_at IS NULL "
            "ORDER BY n.updated_at DESC LIMIT $3",
            workspace_id, sorted(topic_ids), _EXISTING_TOPIC,
        )

    by_id: Dict[int, asyncpg.Record] = {}
    for r in (list(recent[:_EXISTING_RECENT]) + list(relevant)
              + list(topic_neighbors) + list(recent[_EXISTING_RECENT:])):
        if r["id"] not in by_id:
            by_id[r["id"]] = r
            if len(by_id) >= _EXISTING_CAP:
                break
    return list(by_id.values())


async def run_formation(
    document_id: int, timer: Optional["StageTimer"] = None
) -> FormationOutcome:
    """Extract memory from one document and persist it (status flips to
    'complete' inside the persist transaction). Returns the decision node ids
    created/updated (for consolidation) plus a validation report on what
    _persist had to silently repair. Raises on failure — retry, backoff, and
    status bookkeeping live in memory.worker. When a StageTimer is passed,
    laps `fetch` / `llm` / `persist` for SLO reporting."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        doc = await conn.fetchrow("SELECT * FROM documents WHERE id=$1", document_id)
        if doc is None:
            return FormationOutcome()
        chunks = await conn.fetch(
            "SELECT id, chunk_index, text FROM chunks WHERE document_id=$1 ORDER BY chunk_index",
            document_id,
        )
        existing = await _fetch_existing(conn, doc["workspace_id"], document_id,
                                         doc_tags=list(doc["tags"] or []))
    prompt = _build_user_prompt(doc, chunks, existing)
    if timer:
        timer.lap("fetch")
    result = await llm.structured_call(FORMATION_SYSTEM, prompt, FORMATION_SCHEMA)
    if timer:
        timer.lap("llm")
    valid_ids = {r["id"] for r in existing}
    report = validation.validate_extraction(
        result, valid_ids, {c["chunk_index"] for c in chunks},
        config.VALIDATION_MIN_REASONING_CHARS,
    )
    if report["invalid_cross_refs"]:
        log.warning("doc %d extraction referenced %d unknown node ids "
                    "(dropped by safe_node)", document_id, report["invalid_cross_refs"])
    async with pool.acquire() as conn:
        async with conn.transaction():
            provider = llm.active_provider()
            model = llm.active_model()
            run_id = await observations.create_candidate_run(
                conn, doc, model_provider=provider, model_name=model, validation=report,
            )
            batch = await observations.persist_observations(
                conn, run_id, doc, chunks, result,
                model_provider=provider, model_name=model,
            )
            touched = await _persist(
                conn, doc["workspace_id"], document_id, chunks, batch.valid_result,
                valid_ids, doc_tags=list(doc["tags"] or []),
            )
    if timer:
        timer.lap("persist")
    return FormationOutcome(touched=touched, validation=report)
