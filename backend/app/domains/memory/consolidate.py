"""Post-formation consolidation: merge near-duplicate decisions.

Label-exact dedup misses paraphrases ("Choose PostgreSQL as Primary Database
for V1" vs "Data Layer Architecture - PostgreSQL Selection"). After each
formation we embed the signatures (label + summary head) of the decisions that
formation touched, store the vectors on the nodes (memory_nodes.embedding),
and compare them against every decision in the workspace — merging pairs whose
cosine similarity clears MERGE_SIM_THRESHOLD. The older node survives and
accretes the newer one's evidence, edges, and data.

Storing the vectors keeps this incremental: only touched decisions (plus
legacy rows that have never been embedded — lazily backfilled here) pay an
embedding call, instead of re-embedding the whole workspace on every
formation. The stored signatures also let formation pick its existing-memory
digest by relevance to the new document.
"""

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from app.core import config, db, usage
from app.providers.embeddings import (
    EmbeddingSpaceMismatch,
    active_embed_model,
    embed_texts,
    to_pgvector,
)
from app.domains.query import embedding_versions
from . import graph, resolver

log = logging.getLogger("ybase.consolidate")


def _cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # embeddings are L2-normalized


def _signature(label: str, summary: str) -> str:
    return f"{label}\n{(summary or '')[:300]}"


def _parse_vector(raw: str) -> List[float]:
    # pgvector comes back as its text form ("[0.1,0.2,...]") — no codec is
    # registered for the type (see core/db._init_conn).
    return [float(x) for x in raw.strip()[1:-1].split(",") if x]


def similar_pairs_against(
    targets: List[Tuple[int, List[float]]],
    items: List[Tuple[int, List[float]]],
    threshold: float,
) -> List[Tuple[int, int, float]]:
    """(keep_id, drop_id, sim) comparing each target against every item, above
    threshold; ids ordered so the older (smaller id) node is kept. Self-pairs
    are skipped and symmetric duplicates deduped. Pure — kept separate from
    the DB so tests can drive it directly."""
    pairs = []
    seen = set()
    for tid, tvec in targets:
        for iid, ivec in items:
            if iid == tid:
                continue
            key = (tid, iid) if tid < iid else (iid, tid)
            if key in seen:
                continue
            seen.add(key)
            sim = _cosine(tvec, ivec)
            if sim >= threshold:
                pairs.append((key[0], key[1], round(sim, 3)))
    return pairs


def similar_pairs(
    items: List[Tuple[int, List[float]]], threshold: float
) -> List[Tuple[int, int, float]]:
    """Full pairwise pass — the incremental path is similar_pairs_against."""
    return similar_pairs_against(items, items, threshold)


async def _store_embeddings(
    rows, vecs: List[List[float]], workspace_id: int, embedding_model_id: int
) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r, v in zip(rows, vecs):
                await conn.execute(
                    "INSERT INTO memory_node_embeddings("
                    "workspace_id, node_id, embedding_model_id, embedding) "
                    "VALUES($1, $2, $3, $4::vector) "
                    "ON CONFLICT (node_id, embedding_model_id) DO UPDATE "
                    "SET embedding=EXCLUDED.embedding, created_at=now()",
                    workspace_id, r["id"], embedding_model_id, to_pgvector(v),
                )
                # Kept temporarily for existing operational tooling and a
                # low-risk migration rollback; semantic comparisons below use
                # memory_node_embeddings exclusively.
                await conn.execute(
                    "UPDATE memory_nodes SET embedding=$2::vector WHERE id=$1",
                    r["id"], to_pgvector(v),
                )


# ── Debounce queue (batch consolidation) ─────────────────────────────────────


async def enqueue_touched(workspace_id: int, touched: Iterable[int]) -> None:
    """Accumulate a formation's touched decision ids for a later batch run.
    One row per workspace; ids union-merge on conflict."""
    ids = sorted(set(touched))
    if not ids:
        return
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO consolidation_queue(workspace_id, touched_ids) "
            "VALUES($1, $2::int[]) "
            "ON CONFLICT (workspace_id) DO UPDATE SET "
            "  touched_ids = (SELECT array_agg(DISTINCT x ORDER BY x) "
            "                 FROM unnest(consolidation_queue.touched_ids "
            "                             || EXCLUDED.touched_ids) AS x), "
            "  last_touched_at = now()",
            workspace_id, ids,
        )


async def claim_due() -> Optional[Tuple[int, List[int]]]:
    """Claim one due workspace batch: quiet for DEBOUNCE seconds, or waiting
    MAX_DELAY since its first touch (continuous ingest can't starve it).
    Workspaces with a document mid-formation are skipped — consolidation
    deleting nodes under a running _persist would break its FK writes."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE consolidation_queue SET running_since=now() "
            "WHERE workspace_id = ("
            "  SELECT q.workspace_id FROM consolidation_queue q "
            "  WHERE q.running_since IS NULL "
            "  AND (q.last_touched_at < now() - ($1 || ' seconds')::interval "
            "       OR q.first_touched_at < now() - ($2 || ' seconds')::interval) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM documents p WHERE p.workspace_id = q.workspace_id "
            "    AND p.formation_status='processing'"
            "  ) "
            "  ORDER BY q.first_touched_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING workspace_id, touched_ids",
            str(config.CONSOLIDATION_DEBOUNCE_S), str(config.CONSOLIDATION_MAX_DELAY_S),
        )
    if row is None:
        return None
    return row["workspace_id"], list(row["touched_ids"] or [])


async def finish(workspace_id: int) -> None:
    """Complete a claimed batch. The row is deleted unless new touches landed
    mid-run (e.g. a manual re-form) — those stay queued for the next round."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM consolidation_queue WHERE workspace_id=$1 "
            "AND last_touched_at <= running_since",
            workspace_id,
        )
        await conn.execute(
            "UPDATE consolidation_queue SET running_since=NULL, first_touched_at=now() "
            "WHERE workspace_id=$1",
            workspace_id,
        )


async def release(workspace_id: int) -> None:
    """Failed/aborted run: claimable again after a fresh debounce —
    last_touched_at=now() prevents a hot retry loop on a persistent failure."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE consolidation_queue SET running_since=NULL, last_touched_at=now() "
            "WHERE workspace_id=$1",
            workspace_id,
        )


async def reset_stale_runs() -> int:
    """Janitor duty: a crashed instance leaves running_since set forever;
    reset once it outlives the batch timeout (plus margin)."""
    pool = await db.get_pool()
    stale_s = int(config.CONSOLIDATION_TASK_TIMEOUT_S) + 120
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE consolidation_queue SET running_since=NULL, last_touched_at=now() "
            "WHERE running_since IS NOT NULL "
            "AND running_since < now() - ($1 || ' seconds')::interval "
            "RETURNING workspace_id",
            str(stale_s),
        )
    if rows:
        log.warning("janitor reset %d stale consolidation runs: %s",
                    len(rows), [r["workspace_id"] for r in rows])
    return len(rows)


async def merge_similar_decisions(
    workspace_id: int, touched_ids: Optional[Iterable[int]] = None
) -> List[Dict]:
    """Merge near-duplicate decisions in a workspace.

    `touched_ids` are the decision nodes the current formation created or
    updated — only those (plus never-embedded legacy rows) are re-embedded and
    compared against the rest. None means treat every decision as touched
    (full pass, e.g. after a bulk re-embed)."""
    usage_token = usage.set_context(workspace_id=workspace_id, surface="consolidation")
    try:
        return await _merge_similar_decisions(workspace_id, touched_ids)
    finally:
        usage.reset_context(usage_token)


async def _merge_similar_decisions(
    workspace_id: int, touched_ids: Optional[Iterable[int]] = None
) -> List[Dict]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        provider_model_key = await active_embed_model()
        model_id = await embedding_versions.ensure_model(conn, provider_model_key)
        active_model_id = await embedding_versions.active_model(conn, workspace_id)
        if active_model_id is not None and active_model_id != model_id:
            current_key = await embedding_versions.model_key(conn, active_model_id)
            raise EmbeddingSpaceMismatch(provider_model_key, [current_key or "unknown"])
        rows = await conn.fetch(
            "SELECT n.id, n.label, n.summary, ne.embedding::text AS embedding "
            "FROM memory_nodes n LEFT JOIN memory_node_embeddings ne "
            "ON ne.node_id=n.id AND ne.workspace_id=n.workspace_id "
            "AND ne.embedding_model_id=$2 "
            "WHERE n.workspace_id=$1 AND n.kind='decision' AND n.archived_at IS NULL "
            "ORDER BY n.id",
            workspace_id, model_id,
        )
    if len(rows) < 2:
        return []
    touched = set(touched_ids) if touched_ids is not None else {r["id"] for r in rows}
    need = [r for r in rows if r["id"] in touched or r["embedding"] is None]
    fresh: Dict[int, List[float]] = {}
    if need:
        vecs = await embed_texts([_signature(r["label"], r["summary"] or "") for r in need])
        await _store_embeddings(need, vecs, workspace_id, model_id)
        fresh = {r["id"]: v for r, v in zip(need, vecs)}
    if active_model_id is None:
        async with pool.acquire() as conn:
            await embedding_versions.activate_model(conn, workspace_id, model_id)
    # Candidate search via the HNSW index on memory_nodes.embedding — each
    # fresh node fetches its nearest stored decisions instead of a full
    # pairwise pass over the workspace (similar_pairs_against remains the
    # pure-python reference used by tests). Older (smaller) id is kept.
    pairs: List[Tuple[int, int, float]] = []
    seen_pairs = set()
    async with pool.acquire() as conn:
        for nid in sorted(fresh):
            cands = await conn.fetch(
                "SELECT n.id, 1 - (ne.embedding <=> $3::vector) AS sim "
                "FROM memory_node_embeddings ne JOIN memory_nodes n ON n.id=ne.node_id "
                "WHERE ne.workspace_id=$1 AND ne.embedding_model_id=$2 "
                "AND n.workspace_id=$1 AND n.kind='decision' AND n.archived_at IS NULL "
                "AND n.id <> $4 ORDER BY ne.embedding <=> $3::vector LIMIT 8",
                workspace_id, model_id, to_pgvector(fresh[nid]), nid,
            )
            for c in cands:
                if float(c["sim"]) < config.MERGE_SIM_THRESHOLD:
                    continue  # HNSW ordering is approximate — check all 8
                key = (nid, c["id"]) if nid < c["id"] else (c["id"], nid)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    pairs.append((key[0], key[1], round(float(c["sim"]), 3)))
    pairs.sort()

    merged: List[Dict] = []
    dropped = set()
    by_id = {r["id"]: r["label"] for r in rows}
    from app.domains.auth import service as auth  # lazy: avoid import cycle

    async with pool.acquire() as conn:
        async with conn.transaction():
            for keep, drop, sim in pairs:
                if keep in dropped or drop in dropped:
                    continue
                ledger_id = await resolver.record_merge_candidate(
                    conn, workspace_id, keep, drop, sim
                )
                # Merges delete a node — audit inside the same transaction so
                # a merge can always be explained (and recovered) later.
                await auth.audit(
                    conn, "consolidation_merge_candidate", workspace_id, None,
                    target_type="memory_node", target_id=keep,
                    data={"ledger_id": ledger_id, "candidate": drop,
                          "candidate_label": by_id[drop], "survivor_label": by_id[keep], "sim": sim},
                )
                merged.append({
                    "kept": keep, "kept_label": by_id[keep],
                    "dropped": drop, "dropped_label": by_id[drop], "sim": sim,
                })
                log.info("merged decision %s into %s (sim %.3f)", drop, keep, sim)

    if merged:
        # A merge may have replaced the kept node's summary with the longer
        # one, so its stored signature is stale — refresh it.
        kept_ids = sorted({m["kept"] for m in merged})
        async with pool.acquire() as conn:
            kept_rows = await conn.fetch(
                "SELECT id, label, summary FROM memory_nodes "
                "WHERE id = ANY($1::int[]) AND archived_at IS NULL",
                kept_ids,
            )
        if kept_rows:
            vecs = await embed_texts(
                [_signature(r["label"], r["summary"] or "") for r in kept_rows]
            )
            await _store_embeddings(kept_rows, vecs, workspace_id, model_id)
    return merged
