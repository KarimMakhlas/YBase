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

from app.core import config, db
from app.providers.embeddings import embed_texts, to_pgvector
from . import graph

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


async def _store_embeddings(rows, vecs: List[List[float]]) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r, v in zip(rows, vecs):
                await conn.execute(
                    "UPDATE memory_nodes SET embedding=$2::vector WHERE id=$1",
                    r["id"], to_pgvector(v),
                )


async def merge_similar_decisions(
    workspace_id: int, touched_ids: Optional[Iterable[int]] = None
) -> List[Dict]:
    """Merge near-duplicate decisions in a workspace.

    `touched_ids` are the decision nodes the current formation created or
    updated — only those (plus never-embedded legacy rows) are re-embedded and
    compared against the rest. None means treat every decision as touched
    (full pass, e.g. after a bulk re-embed)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, summary, embedding::text AS embedding FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL ORDER BY id",
            workspace_id,
        )
    if len(rows) < 2:
        return []
    touched = set(touched_ids) if touched_ids is not None else {r["id"] for r in rows}
    need = [r for r in rows if r["id"] in touched or r["embedding"] is None]
    fresh: Dict[int, List[float]] = {}
    if need:
        vecs = await embed_texts([_signature(r["label"], r["summary"] or "") for r in need])
        await _store_embeddings(need, vecs)
        fresh = {r["id"]: v for r, v in zip(need, vecs)}
    vec_by_id: Dict[int, List[float]] = dict(fresh)
    for r in rows:
        if r["id"] not in vec_by_id and r["embedding"] is not None:
            vec_by_id[r["id"]] = _parse_vector(r["embedding"])

    everyone = sorted(vec_by_id.items())
    targets = [(i, v) for i, v in everyone if i in fresh]
    pairs = similar_pairs_against(targets, everyone, config.MERGE_SIM_THRESHOLD)

    merged: List[Dict] = []
    dropped = set()
    by_id = {r["id"]: r["label"] for r in rows}
    async with pool.acquire() as conn:
        async with conn.transaction():
            for keep, drop, sim in pairs:
                if keep in dropped or drop in dropped:
                    continue
                await graph.merge_nodes(conn, keep, drop)
                dropped.add(drop)
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
            await _store_embeddings(kept_rows, vecs)
    return merged
