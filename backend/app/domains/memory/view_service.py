"""Read-only product views over the memory graph: decisions, timeline, graph,
nodes, search, people, stats."""

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core import config, db
from app.core.dates import iso_date
from app.domains.auth import service as auth
from app.domains.memory.scoring import node_score

router = APIRouter(prefix="/api", tags=["memory"])


async def _build_decision(conn, row, workspace_id: int) -> Dict[str, Any]:
    """Assemble the full decision payload (reasoning, people, lineage, citations)
    for one decision node. Shared by the list view and the public share view."""
    edges = await conn.fetch(
        "SELECT e.relation, e.src, e.dst, n.id AS other_id, n.kind AS other_kind, "
        "       n.label AS other_label, n.status AS other_status, n.data AS other_data "
        "FROM memory_edges e "
        "JOIN memory_nodes n ON n.id = CASE WHEN e.src=$1 THEN e.dst ELSE e.src END "
        "WHERE e.workspace_id=$2 AND (e.src=$1 OR e.dst=$1) "
        "AND n.archived_at IS NULL",
        row["id"], workspace_id,
    )
    topics = sorted({e["other_label"] for e in edges
                     if e["other_kind"] == "topic" and e["relation"] == "about"})
    people = sorted({e["other_label"] for e in edges
                     if e["other_kind"] == "entity" and e["relation"] == "involves"})
    related = [
        {
            "relation": e["relation"],
            "direction": "out" if e["src"] == row["id"] else "in",
            "node_id": e["other_id"],
            "kind": e["other_kind"],
            "label": e["other_label"],
            "status": e["other_status"],
            "date": (e["other_data"] or {}).get("date"),
        }
        for e in edges
        if e["other_kind"] in ("decision", "question")
    ]
    sources = await conn.fetch(
        "SELECT DISTINCT d.id, d.source, d.title, d.author, d.doc_created_at "
        "FROM chunk_links cl JOIN chunks c ON c.id = cl.chunk_id "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE cl.node_id = $1 AND d.workspace_id=$2 "
        "ORDER BY d.doc_created_at NULLS LAST",
        row["id"], workspace_id,
    )
    data = row["data"] or {}
    first_seen = iso_date(sources[0]["doc_created_at"]) if sources else None
    return {
        "id": row["id"],
        "title": row["label"],
        "summary": row["summary"],
        "status": row["status"],
        "date": data.get("date") or first_seen,
        "confidence": node_score(row["status"], data, evidence_count=len(sources),
                                 fallback_date=first_seen),
        "made_by": data.get("made_by", []),
        "positions": data.get("positions", []),
        "alternatives_considered": data.get("alternatives_considered", []),
        "topics": topics,
        "people": people,
        "related": related,
        "sources": [
            {
                "document_id": s["id"],
                "source": s["source"],
                "title": s["title"],
                "author": s["author"],
                "date": iso_date(s["doc_created_at"]),
            }
            for s in sources
        ],
    }


@router.get("/decisions")
async def list_decisions(
    topic: Optional[str] = None,
    person: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, summary, status, data, created_at, updated_at "
            "FROM memory_nodes WHERE workspace_id=$1 AND kind='decision' "
            "AND archived_at IS NULL "
            "ORDER BY updated_at DESC",
            current.workspace_id,
        )
        decisions = [await _build_decision(conn, r, current.workspace_id) for r in rows]

    def keep(d: Dict[str, Any]) -> bool:
        if topic and topic.lower() not in [t.lower() for t in d["topics"]]:
            return False
        if person and person.lower() not in [p.lower() for p in d["people"] + d["made_by"]]:
            return False
        if status and d["status"] != status:
            return False
        if q:
            hay = " ".join([d["title"], d["summary"] or ""]).lower()
            if q.lower() not in hay:
                return False
        return True

    return [d for d in decisions if keep(d)]


def _share_path(token: str) -> str:
    return f"/#/shared/{token}"


async def _decision_node(conn, node_id: int, workspace_id: int):
    node = await conn.fetchrow(
        "SELECT id, label, summary, status, data FROM memory_nodes "
        "WHERE id=$1 AND workspace_id=$2 AND kind='decision' AND archived_at IS NULL",
        node_id, workspace_id,
    )
    if node is None:
        raise HTTPException(404, "decision not found")
    return node


@router.get("/decisions/{node_id}/share")
async def get_decision_share(
    node_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await _decision_node(conn, node_id, current.workspace_id)
        share = await conn.fetchrow(
            "SELECT token, view_count FROM decision_shares "
            "WHERE node_id=$1 AND workspace_id=$2 AND revoked_at IS NULL",
            node_id, current.workspace_id,
        )
    if share is None:
        return {"shared": False}
    return {"shared": True, "token": share["token"], "path": _share_path(share["token"]),
            "view_count": share["view_count"]}


@router.post("/decisions/{node_id}/share")
async def create_decision_share(
    node_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _decision_node(conn, node_id, current.workspace_id)
            existing = await conn.fetchrow(
                "SELECT token, view_count FROM decision_shares "
                "WHERE node_id=$1 AND workspace_id=$2 AND revoked_at IS NULL",
                node_id, current.workspace_id,
            )
            if existing is not None:
                token, views = existing["token"], existing["view_count"]
            else:
                token = secrets.token_urlsafe(16)
                await conn.execute(
                    "INSERT INTO decision_shares(workspace_id, node_id, token, created_by) "
                    "VALUES($1, $2, $3, $4)",
                    current.workspace_id, node_id, token, current.user_id,
                )
                views = 0
                await auth.audit(conn, "decision_share_create", current.workspace_id,
                                 current.user_id, "memory_node", node_id)
    return {"token": token, "path": _share_path(token), "view_count": views}


@router.delete("/decisions/{node_id}/share")
async def revoke_decision_share(
    node_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE decision_shares SET revoked_at=now() "
                "WHERE node_id=$1 AND workspace_id=$2 AND revoked_at IS NULL RETURNING id",
                node_id, current.workspace_id,
            )
            if row is not None:
                await auth.audit(conn, "decision_share_revoke", current.workspace_id,
                                 current.user_id, "memory_node", node_id)
    return {"revoked": row is not None}


@router.get("/shared/decisions/{token}")
async def shared_decision(token: str) -> Dict[str, Any]:
    """Public, unauthenticated read-only view of one shared decision."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        share = await conn.fetchrow(
            "SELECT s.id, s.node_id, s.workspace_id, w.name AS workspace_name "
            "FROM decision_shares s JOIN workspaces w ON w.id = s.workspace_id "
            "WHERE s.token=$1 AND s.revoked_at IS NULL",
            token,
        )
        if share is None:
            raise HTTPException(404, "shared decision not found")
        node = await conn.fetchrow(
            "SELECT id, label, summary, status, data FROM memory_nodes "
            "WHERE id=$1 AND workspace_id=$2 AND kind='decision' AND archived_at IS NULL",
            share["node_id"], share["workspace_id"],
        )
        if node is None:
            raise HTTPException(404, "shared decision is no longer available")
        decision = await _build_decision(conn, node, share["workspace_id"])
        await conn.execute(
            "UPDATE decision_shares SET view_count = view_count + 1 WHERE id=$1", share["id"]
        )
    # Public payload: citations as metadata only (no document ids / internal
    # graph), so a shared link never exposes other unshared memory.
    decision.pop("related", None)
    decision["sources"] = [
        {"source": s["source"], "title": s["title"], "author": s["author"], "date": s["date"]}
        for s in decision["sources"]
    ]
    return {"workspace_name": share["workspace_name"], "decision": decision}


@router.get("/timeline")
async def timeline(
    limit: int = 500,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    """Newest `limit` events. Unbounded, this pulled every document AND every
    decision/question node in the workspace on each request — fine at demo
    scale, unusable once a real corpus lands. Each side is capped, then merged
    and re-sorted, so the returned window is the most recent activity."""
    limit = max(1, min(limit, 2000))
    pool = await db.get_pool()
    events: List[Dict[str, Any]] = []
    async with pool.acquire() as conn:
        docs = await conn.fetch(
            "SELECT id, source, title, author, doc_created_at, context_summary, "
            "       formation_status FROM documents WHERE workspace_id=$1 "
            "ORDER BY doc_created_at DESC NULLS LAST, id DESC LIMIT $2",
            current.workspace_id, limit,
        )
        for d in docs:
            events.append({
                "type": "document",
                "id": d["id"],
                "date": iso_date(d["doc_created_at"]),
                "source": d["source"],
                "title": d["title"],
                "author": d["author"],
                "summary": d["context_summary"],
                "status": d["formation_status"],
            })
        nodes = await conn.fetch(
            "SELECT n.id, n.kind, n.label, n.status, n.data, "
            "       min(d.doc_created_at) AS first_seen "
            "FROM memory_nodes n "
            "JOIN chunk_links cl ON cl.node_id = n.id "
            "JOIN chunks c ON c.id = cl.chunk_id "
            "JOIN documents d ON d.id = c.document_id "
            "WHERE n.workspace_id=$1 AND d.workspace_id=$1 "
            "AND n.kind IN ('decision', 'question') "
            "AND n.archived_at IS NULL "
            "GROUP BY n.id "
            "ORDER BY min(d.doc_created_at) DESC NULLS LAST, n.id DESC LIMIT $2",
            current.workspace_id, limit,
        )
        revisited = {
            r["dst"] for r in await conn.fetch(
                "SELECT DISTINCT dst FROM memory_edges "
                "JOIN memory_nodes s ON s.id=memory_edges.src "
                "JOIN memory_nodes d ON d.id=memory_edges.dst "
                "WHERE memory_edges.workspace_id=$1 AND relation='revisits' "
                "AND s.archived_at IS NULL AND d.archived_at IS NULL",
                current.workspace_id,
            )
        }
        for n in nodes:
            data = n["data"] or {}
            date = data.get("date")
            if not date and n["first_seen"]:
                date = iso_date(n["first_seen"])
            events.append({
                "type": n["kind"],
                "id": n["id"],
                "date": date,
                "title": n["label"],
                "status": n["status"],
                "revisited": n["id"] in revisited,
                "people": data.get("made_by") or data.get("raised_by") or [],
            })
    events.sort(key=lambda e: (e["date"] or "9999-12-31", e["type"]))
    return events


@router.get("/graph")
async def get_graph(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            "SELECT id, kind, label, summary, status, data FROM memory_nodes "
            "WHERE workspace_id=$1 AND archived_at IS NULL ORDER BY id",
            current.workspace_id,
        )
        edges = await conn.fetch(
            "SELECT e.src, e.dst, e.relation FROM memory_edges e "
            "JOIN memory_nodes s ON s.id=e.src "
            "JOIN memory_nodes d ON d.id=e.dst "
            "WHERE e.workspace_id=$1 AND s.archived_at IS NULL "
            "AND d.archived_at IS NULL ORDER BY e.id",
            current.workspace_id,
        )
    return {"nodes": [dict(n) for n in nodes], "edges": [dict(e) for e in edges]}


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """One memory node with its evidence documents and direct neighbors —
    powers graph focus mode and cross-view navigation."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        node = await conn.fetchrow(
            "SELECT id, kind, label, summary, status, data, created_at, updated_at "
            "FROM memory_nodes WHERE id=$1 AND workspace_id=$2 AND archived_at IS NULL",
            node_id, current.workspace_id,
        )
        if node is None:
            raise HTTPException(404, "node not found")
        sources = await conn.fetch(
            "SELECT DISTINCT d.id, d.source, d.title, d.author, d.doc_created_at "
            "FROM chunk_links cl JOIN chunks c ON c.id = cl.chunk_id "
            "JOIN documents d ON d.id = c.document_id "
            "WHERE cl.node_id=$1 AND d.workspace_id=$2 "
            "ORDER BY d.doc_created_at NULLS LAST",
            node_id, current.workspace_id,
        )
        neighbors = await conn.fetch(
            "SELECT e.relation, e.src, e.dst, n.id, n.kind, n.label, n.status "
            "FROM memory_edges e "
            "JOIN memory_nodes n ON n.id = CASE WHEN e.src=$1 THEN e.dst ELSE e.src END "
            "WHERE e.workspace_id=$2 AND (e.src=$1 OR e.dst=$1) "
            "AND n.archived_at IS NULL",
            node_id, current.workspace_id,
        )
    out = dict(node)
    out["sources"] = [
        {
            "document_id": s["id"], "source": s["source"], "title": s["title"],
            "author": s["author"],
            "date": iso_date(s["doc_created_at"]),
        }
        for s in sources
    ]
    out["neighbors"] = [
        {
            "node_id": n["id"], "kind": n["kind"], "label": n["label"],
            "status": n["status"], "relation": n["relation"],
            "direction": "out" if n["src"] == node_id else "in",
        }
        for n in neighbors
    ]
    return out


@router.get("/search")
async def search(
    q: str,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    """Cmd-K palette search across memory nodes and documents."""
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            "SELECT id, kind, label, status, summary FROM memory_nodes "
            "WHERE workspace_id=$1 AND archived_at IS NULL "
            "AND (label ILIKE $2 OR summary ILIKE $2) "
            "ORDER BY (label ILIKE $2) DESC, updated_at DESC LIMIT 12",
            current.workspace_id, like,
        )
        docs = await conn.fetch(
            "SELECT id, source, title, author, doc_created_at FROM documents "
            "WHERE workspace_id=$1 AND (title ILIKE $2 OR author ILIKE $2) "
            "ORDER BY doc_created_at DESC NULLS LAST LIMIT 8",
            current.workspace_id, like,
        )
    results = [
        {
            "type": n["kind"], "id": n["id"], "title": n["label"], "status": n["status"],
            "detail": (n["summary"] or "")[:140],
        }
        for n in nodes
    ] + [
        {
            "type": "document", "id": d["id"], "title": d["title"], "status": None,
            "detail": f"{d['source']} · {d['author'] or 'unknown'}"
            + (f" · {d['doc_created_at'].date().isoformat()}" if d["doc_created_at"] else ""),
        }
        for d in docs
    ]
    return results


@router.get("/people")
async def list_people(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT n.id, n.label, n.summary, n.data, "
            "  (SELECT count(*) FROM memory_edges e JOIN memory_nodes d "
            "     ON d.id = CASE WHEN e.src=n.id THEN e.dst ELSE e.src END "
            "   WHERE (e.src=n.id OR e.dst=n.id) AND e.relation='involves' "
            "     AND e.workspace_id=$1 AND d.workspace_id=$1 AND d.kind='decision' "
            "     AND d.archived_at IS NULL) AS decision_count, "
            "  (SELECT count(*) FROM memory_edges e JOIN memory_nodes d "
            "     ON d.id = CASE WHEN e.src=n.id THEN e.dst ELSE e.src END "
            "   WHERE (e.src=n.id OR e.dst=n.id) AND e.relation='raised_by' "
            "     AND e.workspace_id=$1 AND d.workspace_id=$1 AND d.kind='question' "
            "     AND d.archived_at IS NULL) AS question_count "
            "FROM memory_nodes n "
            "WHERE n.workspace_id=$1 AND n.kind='entity' "
            "AND n.archived_at IS NULL "
            "AND COALESCE(n.data->>'entity_kind', 'person')='person' "
            "ORDER BY decision_count DESC, n.label",
            current.workspace_id,
        )
    return [
        {
            "id": r["id"], "name": r["label"], "summary": r["summary"],
            "decisions": r["decision_count"], "questions": r["question_count"],
        }
        for r in rows
    ]


@router.get("/people/{node_id}")
async def person_detail(
    node_id: int,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Everything one person advocated, decided, or raised — with their
    recorded positions pulled out of each decision."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        person = await conn.fetchrow(
            "SELECT id, label, summary, data FROM memory_nodes "
            "WHERE id=$1 AND workspace_id=$2 AND kind='entity' "
            "AND archived_at IS NULL",
            node_id, current.workspace_id,
        )
        if person is None:
            raise HTTPException(404, "person not found")
        related = await conn.fetch(
            "SELECT e.relation, n.id, n.kind, n.label, n.status, n.data "
            "FROM memory_edges e "
            "JOIN memory_nodes n ON n.id = CASE WHEN e.src=$1 THEN e.dst ELSE e.src END "
            "WHERE e.workspace_id=$2 AND (e.src=$1 OR e.dst=$1) "
            "AND n.kind IN ('decision', 'question') "
            "AND n.archived_at IS NULL "
            "ORDER BY n.updated_at DESC",
            node_id, current.workspace_id,
        )
        docs = await conn.fetch(
            "SELECT DISTINCT d.id, d.source, d.title, d.doc_created_at FROM chunk_links cl "
            "JOIN chunks c ON c.id = cl.chunk_id JOIN documents d ON d.id = c.document_id "
            "WHERE cl.node_id=$1 AND d.workspace_id=$2 ORDER BY d.doc_created_at NULLS LAST",
            node_id, current.workspace_id,
        )
    name_lower = person["label"].lower()
    decisions, questions = [], []
    for r in related:
        data = r["data"] or {}
        item = {
            "node_id": r["id"], "title": r["label"], "status": r["status"],
            "date": data.get("date"), "relation": r["relation"],
            "positions": [p for p in data.get("positions", [])
                          if name_lower in p.lower()],
        }
        if r["kind"] == "decision":
            decisions.append(item)
        else:
            questions.append(item)
    return {
        "id": person["id"],
        "name": person["label"],
        "summary": person["summary"],
        "decisions": decisions,
        "questions": questions,
        "documents": [
            {
                "document_id": d["id"], "source": d["source"], "title": d["title"],
                "date": iso_date(d["doc_created_at"]),
            }
            for d in docs
        ],
    }


@router.get("/stats")
async def stats(
    since: Optional[str] = None,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT "
            "  (SELECT count(*) FROM documents WHERE workspace_id=$1) AS documents, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='decision' "
            "     AND archived_at IS NULL) AS decisions, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='question' "
            "     AND status='open' AND archived_at IS NULL) "
            "    AS open_questions, "
            "  (SELECT count(*) FROM memory_nodes WHERE workspace_id=$1 AND kind='entity' "
            "     AND archived_at IS NULL) AS entities",
            current.workspace_id,
        )
        recent = await conn.fetch(
            "SELECT id, label, summary, status, data, updated_at FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 5",
            current.workspace_id,
        )
        questions = await conn.fetch(
            "SELECT id, label, status, data, created_at FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='question' AND status='open' "
            "AND archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 5",
            current.workspace_id,
        )
        stale = await conn.fetch(
            "SELECT id, label, data, created_at FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='question' AND status='open' "
            "AND archived_at IS NULL "
            "AND created_at < now() - ($2 || ' days')::interval "
            "ORDER BY created_at LIMIT 5",
            current.workspace_id,
            str(config.STALE_QUESTION_DAYS),
        )
        # relitigation: revisit edges seen in the last 14 days
        revisits = await conn.fetch(
            "SELECT e.created_at, s.id AS new_id, s.label AS new_label, s.status AS new_status, "
            "       o.id AS old_id, o.label AS old_label "
            "FROM memory_edges e "
            "JOIN memory_nodes s ON s.id = e.src JOIN memory_nodes o ON o.id = e.dst "
            "WHERE e.workspace_id=$1 AND e.relation='revisits' "
            "AND s.archived_at IS NULL AND o.archived_at IS NULL "
            "AND e.created_at > now() - interval '14 days' "
            "ORDER BY e.created_at DESC LIMIT 5",
            current.workspace_id,
        )
        sources = await conn.fetch(
            "SELECT source, count(*) AS n FROM documents WHERE workspace_id=$1 "
            "GROUP BY source ORDER BY n DESC",
            current.workspace_id,
        )
        digest = None
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                since_dt = None
        if since_dt is not None:
            new_decisions = await conn.fetch(
                "SELECT id, label, status, data FROM memory_nodes "
                "WHERE workspace_id=$1 AND kind='decision' AND created_at > $2 "
                "AND archived_at IS NULL "
                "ORDER BY created_at DESC LIMIT 8",
                current.workspace_id, since_dt,
            )
            resolved = await conn.fetch(
                "SELECT id, label, data FROM memory_nodes "
                "WHERE workspace_id=$1 AND kind='question' AND status='resolved' AND updated_at > $2 "
                "AND archived_at IS NULL "
                "ORDER BY updated_at DESC LIMIT 8",
                current.workspace_id, since_dt,
            )
            opened = await conn.fetch(
                "SELECT id, label FROM memory_nodes "
                "WHERE workspace_id=$1 AND kind='question' AND status='open' AND created_at > $2 "
                "AND archived_at IS NULL "
                "ORDER BY created_at DESC LIMIT 8",
                current.workspace_id, since_dt,
            )
            new_docs = await conn.fetchval(
                "SELECT count(*) FROM documents WHERE workspace_id=$1 AND ingested_at > $2",
                current.workspace_id, since_dt,
            )
            digest = {
                "since": since_dt.isoformat(),
                "new_documents": new_docs,
                "new_decisions": [
                    {"id": r["id"], "title": r["label"], "status": r["status"],
                     "date": (r["data"] or {}).get("date")}
                    for r in new_decisions
                ],
                "resolved_questions": [
                    {"id": r["id"], "title": r["label"],
                     "resolution": (r["data"] or {}).get("resolution")}
                    for r in resolved
                ],
                "opened_questions": [
                    {"id": r["id"], "title": r["label"]} for r in opened
                ],
            }
    return {
        "counts": dict(counts),
        "recent_decisions": [
            {
                "id": r["id"],
                "title": r["label"],
                "summary": r["summary"],
                "status": r["status"],
                "date": (r["data"] or {}).get("date"),
            }
            for r in recent
        ],
        "open_questions": [
            {
                "id": q["id"],
                "title": q["label"],
                "date": (q["data"] or {}).get("date"),
            }
            for q in questions
        ],
        "stale_questions": [
            {
                "id": s["id"],
                "title": s["label"],
                "age_days": (datetime.now(timezone.utc) - s["created_at"]).days,
            }
            for s in stale
        ],
        "revisits": [
            {
                "new_id": r["new_id"], "new_title": r["new_label"],
                "new_status": r["new_status"],
                "old_id": r["old_id"], "old_title": r["old_label"],
                "when": iso_date(r["created_at"]),
            }
            for r in revisits
        ],
        "sources": [dict(s) for s in sources],
        "digest": digest,
    }
