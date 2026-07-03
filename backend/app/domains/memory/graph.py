"""Memory graph primitives: node upserts, typed edges, chunk provenance,
and bounded neighborhood expansion (adjacency tables in Postgres)."""

from typing import Dict, Iterable, List, Optional, Set, Tuple

import asyncpg


def _clean_label(label: str) -> str:
    return " ".join(label.split())[:300]


async def upsert_node(
    conn: asyncpg.Connection,
    workspace_id: int,
    kind: str,
    label: str,
    summary: Optional[str] = None,
    status: Optional[str] = None,
    data: Optional[dict] = None,
) -> int:
    """Create or merge a memory node, deduped on (kind, lower(label)).

    Merging keeps the longer summary, latest status, and unions data keys —
    so the same decision/entity surfacing in multiple documents accretes
    evidence instead of duplicating.
    """
    label = _clean_label(label)
    data = {k: v for k, v in (data or {}).items() if v not in (None, "", [])}
    # Atomic upsert on the (kind, lower(label)) unique index — concurrent
    # formations extracting the same person/topic must not race.
    return await conn.fetchval(
        "INSERT INTO memory_nodes(workspace_id, kind, label, summary, status, data) "
        "VALUES($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (workspace_id, kind, lower(label)) WHERE archived_at IS NULL DO UPDATE SET "
        "  summary = CASE WHEN length(coalesce(EXCLUDED.summary, '')) > "
        "                      length(coalesce(memory_nodes.summary, '')) "
        "            THEN EXCLUDED.summary ELSE memory_nodes.summary END, "
        "  status = COALESCE(EXCLUDED.status, memory_nodes.status), "
        "  data = memory_nodes.data || EXCLUDED.data, "
        "  updated_at = now() "
        "RETURNING id",
        workspace_id, kind, label, summary, status, data,
    )


async def set_status(conn: asyncpg.Connection, node_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE memory_nodes SET status=$2, updated_at=now() "
        "WHERE id=$1 AND archived_at IS NULL",
        node_id, status,
    )


async def merge_data(conn: asyncpg.Connection, node_id: int, data: dict) -> None:
    row = await conn.fetchrow(
        "SELECT data FROM memory_nodes WHERE id=$1 AND archived_at IS NULL", node_id
    )
    if row is None:
        return
    merged = dict(row["data"] or {})
    merged.update({k: v for k, v in data.items() if v not in (None, "", [])})
    await conn.execute(
        "UPDATE memory_nodes SET data=$2, updated_at=now() "
        "WHERE id=$1 AND archived_at IS NULL",
        node_id, merged,
    )


async def add_edge(
    conn: asyncpg.Connection, workspace_id: int, src: int, dst: int, relation: str
) -> None:
    if src == dst:
        return
    await conn.execute(
        "INSERT INTO memory_edges(workspace_id, src, dst, relation) VALUES($1, $2, $3, $4) "
        "ON CONFLICT DO NOTHING",
        workspace_id, src, dst, relation,
    )


async def link_chunk(
    conn: asyncpg.Connection, chunk_id: int, node_id: int, relation: str = "evidence"
) -> None:
    await conn.execute(
        "INSERT INTO chunk_links(chunk_id, node_id, relation) VALUES($1, $2, $3) "
        "ON CONFLICT DO NOTHING",
        chunk_id, node_id, relation,
    )


async def merge_nodes(conn: asyncpg.Connection, keep_id: int, drop_id: int) -> None:
    """Fold drop_id into keep_id: provenance and edges move over (skipping
    duplicates and would-be self-edges), summary/status/data merge, and the
    duplicate node is deleted."""
    if keep_id == drop_id:
        return
    keep_ws = await conn.fetchval(
        "SELECT workspace_id FROM memory_nodes WHERE id=$1 AND archived_at IS NULL",
        keep_id,
    )
    if keep_ws is None:
        return
    await conn.execute(
        "INSERT INTO chunk_links(chunk_id, node_id, relation) "
        "SELECT chunk_id, $2, relation FROM chunk_links WHERE node_id = $1 "
        "ON CONFLICT DO NOTHING",
        drop_id, keep_id,
    )
    await conn.execute(
        "INSERT INTO memory_edges(workspace_id, src, dst, relation) "
        "SELECT $3, $2, dst, relation FROM memory_edges WHERE src = $1 AND dst <> $2 "
        "ON CONFLICT DO NOTHING",
        drop_id, keep_id, keep_ws,
    )
    await conn.execute(
        "INSERT INTO memory_edges(workspace_id, src, dst, relation) "
        "SELECT $3, src, $2, relation FROM memory_edges WHERE dst = $1 AND src <> $2 "
        "ON CONFLICT DO NOTHING",
        drop_id, keep_id, keep_ws,
    )
    keep = await conn.fetchrow(
        "SELECT summary, status, data FROM memory_nodes WHERE id=$1 AND archived_at IS NULL",
        keep_id,
    )
    drop = await conn.fetchrow(
        "SELECT summary, status, data FROM memory_nodes WHERE id=$1 AND archived_at IS NULL",
        drop_id,
    )
    if keep is None or drop is None:
        return
    summary = keep["summary"]
    if drop["summary"] and len(drop["summary"]) > len(summary or ""):
        summary = drop["summary"]
    merged = dict(keep["data"] or {})
    merged.update({k: v for k, v in (drop["data"] or {}).items() if v not in (None, "", [])})
    await conn.execute(
        "UPDATE memory_nodes SET summary=$2, status=COALESCE($3, status), data=$4, "
        "updated_at=now() WHERE id=$1",
        keep_id, summary, drop["status"], merged,
    )
    # CASCADEs clean up the duplicate's remaining edges/links
    await conn.execute("DELETE FROM memory_nodes WHERE id=$1", drop_id)


# When the max_nodes budget bites, spend it on the relations that make this
# memory rather than search: decision history (revisits/resolves) and explicit
# tensions (relates_to) before people, and people before topics. Topic edges
# additionally get a per-node fan-out cap — a popular topic ("database") links
# to every decision in its area, and expanding through such a hub would flood
# the budget with weakly related nodes.
_RELATION_PRIORITY = {
    "revisits": 0, "resolves": 1, "relates_to": 2, "raised_by": 3,
    "involves": 4, "about": 5,
}
_ABOUT_FANOUT_CAP = 5


async def expand(
    conn: asyncpg.Connection,
    workspace_id: int,
    seed_ids: Iterable[int],
    hops: int = 2,
    max_nodes: int = 40,
) -> Tuple[Set[int], List[Dict]]:
    """Breadth-first expansion over memory_edges from seed nodes, admitting
    high-value relations first (see _RELATION_PRIORITY)."""
    nodes: Set[int] = set(seed_ids)
    frontier: Set[int] = set(nodes)
    edges: List[Dict] = []
    seen_edges: Set[int] = set()
    for _ in range(hops):
        if not frontier or len(nodes) >= max_nodes:
            break
        rows = await conn.fetch(
            "SELECT e.id, e.src, e.dst, e.relation FROM memory_edges e "
            "JOIN memory_nodes s ON s.id=e.src "
            "JOIN memory_nodes d ON d.id=e.dst "
            "WHERE e.workspace_id=$1 AND s.archived_at IS NULL AND d.archived_at IS NULL "
            "AND (e.src = ANY($2::int[]) OR e.dst = ANY($2::int[]))",
            workspace_id, list(frontier),
        )
        rows = sorted(rows, key=lambda r: (_RELATION_PRIORITY.get(r["relation"], 9), r["id"]))
        next_frontier: Set[int] = set()
        about_fanout: Dict[int, int] = {}
        for r in rows:
            if r["id"] in seen_edges:
                continue
            origin = r["src"] if r["src"] in frontier else r["dst"]
            other = r["dst"] if origin == r["src"] else r["src"]
            new_via_topic = r["relation"] == "about" and other not in nodes
            if new_via_topic and about_fanout.get(origin, 0) >= _ABOUT_FANOUT_CAP:
                continue  # topic hub: leave budget for other relations/nodes
            seen_edges.add(r["id"])
            edges.append({"src": r["src"], "dst": r["dst"], "relation": r["relation"]})
            if new_via_topic:
                about_fanout[origin] = about_fanout.get(origin, 0) + 1
            for nid in (r["src"], r["dst"]):
                if nid not in nodes and len(nodes) < max_nodes:
                    nodes.add(nid)
                    next_frontier.add(nid)
        frontier = next_frontier
    return nodes, edges
