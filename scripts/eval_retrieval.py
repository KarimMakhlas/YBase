#!/usr/bin/env python3
"""Measure tenant-scoped ANN recall against exact pgvector search.

Usage:
    backend/.venv/bin/python scripts/eval_retrieval.py --workspace default
"""

import argparse
import asyncio
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db  # noqa: E402
from app.domains.query.vector_search import (  # noqa: E402
    approximate_vector_search,
    exact_vector_search,
    recall_at_k,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare tenant ANN retrieval with exact pgvector search."
    )
    parser.add_argument("--workspace", required=True, help="workspace slug")
    parser.add_argument("--queries", type=int, default=50, help="sample size (default: 50)")
    parser.add_argument("--k", type=int, default=10, help="recall cutoff (default: 10)")
    parser.add_argument(
        "--min-recall",
        type=float,
        default=0.95,
        help="minimum acceptable mean recall (default: 0.95)",
    )
    parser.add_argument(
        "--seed",
        default="ybase-retrieval-v1",
        help="deterministic sample salt",
    )
    args = parser.parse_args()
    if args.queries <= 0:
        parser.error("--queries must be positive")
    if args.k <= 0:
        parser.error("--k must be positive")
    if not 0 <= args.min_recall <= 1:
        parser.error("--min-recall must be between 0 and 1")
    return args


async def main() -> int:
    args = parse_args()
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            workspace = await conn.fetchrow(
                "SELECT id, name, slug FROM workspaces WHERE lower(slug)=lower($1)",
                args.workspace,
            )
            if workspace is None:
                print(f"workspace not found: {args.workspace}", file=sys.stderr)
                return 2

            model_counts = await conn.fetch(
                "SELECT embed_model, count(*)::int AS chunk_count "
                "FROM chunks WHERE workspace_id=$1 AND embedding IS NOT NULL "
                "GROUP BY embed_model ORDER BY embed_model",
                workspace["id"],
            )
            insufficient = [
                row for row in model_counts if row["chunk_count"] <= args.k
            ]
            if not model_counts or insufficient:
                print(
                    "insufficient chunk corpus: every embedding model needs at least "
                    f"{args.k + 1} chunks to evaluate recall@{args.k}",
                    file=sys.stderr,
                )
                for row in model_counts:
                    print(
                        f"model={row['embed_model']} chunks={row['chunk_count']}",
                        file=sys.stderr,
                    )
                return 2

            samples = await conn.fetch(
                "SELECT id, embedding::text AS embedding, embed_model "
                "FROM chunks WHERE workspace_id=$1 AND embedding IS NOT NULL "
                "ORDER BY md5(id::text || $2) LIMIT $3",
                workspace["id"],
                args.seed,
                args.queries,
            )

            recalls = []
            iterative_scan_queries = 0
            for sample in samples:
                approximate = await approximate_vector_search(
                    conn,
                    qvec=sample["embedding"],
                    workspace_id=workspace["id"],
                    embed_model=sample["embed_model"],
                    limit=args.k,
                    exclude_chunk_id=sample["id"],
                )
                exact = await exact_vector_search(
                    conn,
                    qvec=sample["embedding"],
                    workspace_id=workspace["id"],
                    embed_model=sample["embed_model"],
                    limit=args.k,
                    exclude_chunk_id=sample["id"],
                )
                if len(exact.rows) < args.k:
                    print(
                        "insufficient eligible neighbors for sampled chunk "
                        f"{sample['id']}",
                        file=sys.stderr,
                    )
                    return 2
                recalls.append(
                    recall_at_k(
                        [row["id"] for row in exact.rows],
                        [row["id"] for row in approximate.rows],
                        args.k,
                    )
                )
                iterative_scan_queries += int(approximate.iterative_scan_enabled)

        mean_recall = statistics.fmean(recalls)
        min_recall = min(recalls)
        print(
            f"workspace={workspace['slug']} ({workspace['name']}) "
            f"samples={len(samples)} k={args.k}"
        )
        for row in model_counts:
            print(f"model={row['embed_model']} chunks={row['chunk_count']}")
        print(
            f"mean_recall@{args.k}={mean_recall:.3f} "
            f"min_recall@{args.k}={min_recall:.3f} "
            f"iterative_scan_queries={iterative_scan_queries}/{len(samples)}"
        )
        return 0 if mean_recall >= args.min_recall else 1
    finally:
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
