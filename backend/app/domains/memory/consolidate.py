"""Post-formation consolidation: merge near-duplicate decisions.

Label-exact dedup misses paraphrases ("Choose PostgreSQL as Primary Database
for V1" vs "Data Layer Architecture - PostgreSQL Selection"). After each
formation we embed every decision's label + summary head and merge pairs whose
cosine similarity clears MERGE_SIM_THRESHOLD — the older node survives and
accretes the newer one's evidence, edges, and data.
"""

import logging
from typing import Dict, List, Tuple

from app.core import config, db
from app.providers.embeddings import embed_texts
from . import graph

log = logging.getLogger("ybase.consolidate")


def _cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # embeddings are L2-normalized


def _signature(label: str, summary: str) -> str:
    return f"{label}\n{(summary or '')[:300]}"


def similar_pairs(
    items: List[Tuple[int, str]], threshold: float
) -> List[Tuple[int, int, float]]:
    """(keep_id, drop_id, sim) for embedded items above threshold; ids ordered
    so the older (smaller id) node is kept. `items` is [(node_id, embedding)]…
    kept separate from the DB so tests can drive it directly."""
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            sim = _cosine(items[i][1], items[j][1])
            if sim >= threshold:
                a, b = items[i][0], items[j][0]
                keep, drop = (a, b) if a < b else (b, a)
                pairs.append((keep, drop, round(sim, 3)))
    return pairs


async def merge_similar_decisions(workspace_id: int) -> List[Dict]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, summary FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL ORDER BY id",
            workspace_id,
        )
    if len(rows) < 2:
        return []
    texts = [_signature(r["label"], r["summary"] or "") for r in rows]
    vecs = await embed_texts(texts)
    pairs = similar_pairs(
        [(r["id"], v) for r, v in zip(rows, vecs)], config.MERGE_SIM_THRESHOLD
    )
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
    return merged
