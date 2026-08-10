#!/usr/bin/env python3
"""Run a deterministic tenant-scoped ANN recall gate for CI/release builds."""

import argparse
import asyncio
import os
import statistics
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db, migrate  # noqa: E402
from app.domains.documents.ingestion import IngestRequest, ingest_document  # noqa: E402
from app.domains.query.vector_search import (  # noqa: E402
    approximate_vector_search,
    exact_vector_search,
    recall_at_k,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed an isolated tenant corpus and enforce ANN recall."
    )
    parser.add_argument("--documents", type=int, default=16)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--min-recall", type=float, default=0.95)
    args = parser.parse_args()
    if args.documents <= args.k:
        parser.error("--documents must be greater than --k")
    if not 0 <= args.min_recall <= 1:
        parser.error("--min-recall must be between 0 and 1")
    return args


def _request(index: int) -> IngestRequest:
    return IngestRequest(
        source="ci",
        title=f"retrieval gate document {index}",
        text=(
            f"Release gate evidence {index}: tenant-scoped vector retrieval must preserve "
            f"nearest neighbors for deterministic corpus item {index}. "
            f"Unique token retrieval-gate-{index:03d}."
        ),
        idempotency_key=f"ci-retrieval-gate-{index}",
    )


async def main() -> int:
    args = parse_args()
    await migrate.run()
    pool = await db.get_pool()
    workspace_id = None
    try:
        slug = f"ci-retrieval-{uuid.uuid4().hex[:12]}"
        async with pool.acquire() as conn:
            workspace_id = await conn.fetchval(
                "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                "CI retrieval gate", slug,
            )
        for index in range(args.documents):
            await ingest_document(_request(index), workspace_id)

        async with pool.acquire() as conn:
            samples = await conn.fetch(
                "SELECT ce.chunk_id AS id, ce.embedding::text AS embedding, ce.embedding_model_id "
                "FROM chunk_embeddings ce WHERE ce.workspace_id=$1 ORDER BY ce.chunk_id",
                workspace_id,
            )
            recalls = []
            for sample in samples:
                approximate = await approximate_vector_search(
                    conn, qvec=sample["embedding"], workspace_id=workspace_id,
                    embedding_model_id=sample["embedding_model_id"], limit=args.k,
                    exclude_chunk_id=sample["id"],
                )
                exact = await exact_vector_search(
                    conn, qvec=sample["embedding"], workspace_id=workspace_id,
                    embedding_model_id=sample["embedding_model_id"], limit=args.k,
                    exclude_chunk_id=sample["id"],
                )
                recalls.append(recall_at_k(
                    [row["id"] for row in exact.rows],
                    [row["id"] for row in approximate.rows], args.k,
                ))
        mean_recall = statistics.fmean(recalls)
        print(
            f"ci_retrieval_gate samples={len(recalls)} k={args.k} "
            f"mean_recall={mean_recall:.3f} required={args.min_recall:.3f}"
        )
        return 0 if mean_recall >= args.min_recall else 1
    finally:
        if workspace_id is not None:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM workspaces WHERE id=$1", workspace_id)
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
