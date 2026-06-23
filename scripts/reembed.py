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
from app.providers.embeddings import active_embedder, embed_texts, to_pgvector  # noqa: E402

BATCH = 32


async def main() -> None:
    provider = await active_embedder()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, text FROM chunks ORDER BY id")
    print(f"Re-embedding {len(rows)} chunks with provider: {provider}")
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        vecs = await embed_texts([r["text"] for r in batch], kind="document")
        async with pool.acquire() as conn:
            async with conn.transaction():
                for r, v in zip(batch, vecs):
                    await conn.execute(
                        "UPDATE chunks SET embedding = $2::vector WHERE id = $1",
                        r["id"], to_pgvector(v),
                    )
        print(f"  {min(start + BATCH, len(rows))}/{len(rows)}")
    await db.close_pool()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
