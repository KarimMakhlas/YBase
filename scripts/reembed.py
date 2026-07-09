#!/usr/bin/env python3
"""Re-embed every chunk with the currently active embedding provider.

Run this after switching embedding providers (e.g. hash → nomic-embed-text):
queries are embedded with the active provider, so stored chunk vectors must
live in the same embedding space or retrieval silently degrades.

Usage:
    cd ybase
    backend/.venv/bin/python scripts/reembed.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db  # noqa: E402
from app.domains.memory.consolidate import _signature  # noqa: E402
from app.providers.embeddings import (  # noqa: E402
    active_embed_model,
    active_embedder,
    embed_texts,
    to_pgvector,
)

BATCH = 32


async def main() -> None:
    provider = await active_embedder()
    model = await active_embed_model()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, text FROM chunks ORDER BY id")
    print(f"Re-embedding {len(rows)} chunks with model: {model}")
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        vecs = await embed_texts([r["text"] for r in batch], kind="document")
        async with pool.acquire() as conn:
            async with conn.transaction():
                for r, v in zip(batch, vecs):
                    await conn.execute(
                        "UPDATE chunks SET embedding = $2::vector, embed_model = $3 WHERE id = $1",
                        r["id"], to_pgvector(v), model,
                    )
        print(f"  {min(start + BATCH, len(rows))}/{len(rows)}")

    # Memory-node signature embeddings (consolidation + formation-context
    # selection) live in the same space as chunk vectors and must move with
    # them. Only rows that already have a vector are refreshed; the rest are
    # lazily backfilled by consolidation.
    async with pool.acquire() as conn:
        nodes = await conn.fetch(
            "SELECT id, label, summary FROM memory_nodes "
            "WHERE embedding IS NOT NULL ORDER BY id"
        )
    print(f"Re-embedding {len(nodes)} memory-node signatures")
    for start in range(0, len(nodes), BATCH):
        batch = nodes[start : start + BATCH]
        vecs = await embed_texts(
            [_signature(r["label"], r["summary"] or "") for r in batch], kind="document"
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                for r, v in zip(batch, vecs):
                    await conn.execute(
                        "UPDATE memory_nodes SET embedding = $2::vector WHERE id = $1",
                        r["id"], to_pgvector(v),
                    )
        print(f"  {min(start + BATCH, len(nodes))}/{len(nodes)}")
    await db.close_pool()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
