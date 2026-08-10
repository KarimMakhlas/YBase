#!/usr/bin/env python3
"""Profile tenant-scoped vector retrieval on a disposable synthetic corpus.

Example for a Neon sizing or pgvector-index change:

    DATABASE_URL=... python scripts/retrieval_load_profile.py \
      --chunks 100000 --queries 50 --max-p95-ms 100
"""

import argparse
import asyncio
import math
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db, migrate  # noqa: E402
from app.domains.query import embedding_versions  # noqa: E402
from app.domains.query.vector_search import approximate_vector_search  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure tenant-scoped ANN p95 latency on a disposable corpus."
    )
    parser.add_argument("--chunks", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--batch", type=int, default=5_000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--max-p95-ms", type=float, default=100.0)
    parser.add_argument(
        "--keep-workspace", action="store_true",
        help="retain the generated workspace for manual database inspection",
    )
    args = parser.parse_args()
    if args.chunks <= args.k:
        parser.error("--chunks must be greater than --k")
    if args.queries <= 0 or args.batch <= 0 or args.k <= 0:
        parser.error("--queries, --batch, and --k must be positive")
    if args.max_p95_ms <= 0:
        parser.error("--max-p95-ms must be positive")
    return args


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


async def main() -> int:
    args = parse_args()
    await migrate.run()
    pool = await db.get_pool()
    workspace_id = None
    slug = f"load-profile-{uuid.uuid4().hex[:12]}"
    # A fixed 512-d vector isolates database/index latency from provider latency.
    vector = "[" + ",".join(["0"] * 512) + "]"
    try:
        async with pool.acquire() as conn:
            model_id = await embedding_versions.ensure_model(
                conn, "load-profile:synthetic:512"
            )
            workspace_id = await conn.fetchval(
                "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                "Retrieval load profile", slug,
            )
            document_id = await conn.fetchval(
                "INSERT INTO documents(workspace_id, source, title, raw_text, formation_status) "
                "VALUES($1, 'load-profile', 'Synthetic retrieval corpus', 'Synthetic corpus', 'complete') "
                "RETURNING id",
                workspace_id,
            )
            for start in range(0, args.chunks, args.batch):
                count = min(args.batch, args.chunks - start)
                await conn.execute(
                    "INSERT INTO chunks(workspace_id, document_id, chunk_index, text, embedding, embed_model) "
                    "SELECT $1, $2, g - 1, 'synthetic retrieval profile chunk ' || g, "
                    "$3::vector, 'load-profile:synthetic:512' "
                    "FROM generate_series($4::int, $5::int) AS g",
                    workspace_id, document_id, vector, start + 1, start + count,
                )
            await conn.execute(
                "INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding) "
                "SELECT workspace_id, id, $2, $3::vector FROM chunks WHERE document_id=$1",
                document_id, model_id, vector,
            )
            await embedding_versions.activate_model(conn, workspace_id, model_id)

            latencies_ms = []
            for _ in range(args.queries):
                started = time.perf_counter()
                result = await approximate_vector_search(
                    conn, qvec=vector, workspace_id=workspace_id,
                    embedding_model_id=model_id, limit=args.k,
                )
                latencies_ms.append((time.perf_counter() - started) * 1000)
                if len(result.rows) != args.k:
                    raise RuntimeError(
                        f"retrieval returned {len(result.rows)} rows, expected {args.k}"
                    )
        p50 = statistics.median(latencies_ms)
        p95 = _p95(latencies_ms)
        print(
            f"retrieval_load_profile workspace={slug} chunks={args.chunks} "
            f"queries={args.queries} p50_ms={p50:.2f} p95_ms={p95:.2f} "
            f"budget_ms={args.max_p95_ms:.2f}"
        )
        return 0 if p95 <= args.max_p95_ms else 1
    finally:
        if workspace_id is not None and not args.keep_workspace:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM workspaces WHERE id=$1", workspace_id)
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
