#!/usr/bin/env python3
"""Exercise durable acceptance, fair claims, and concurrent preprocessing.

Run against staging before changing worker concurrency or Neon connection
capacity. It creates isolated workspaces and removes them automatically.
"""

import argparse
import asyncio
import math
import os
import sys
import time
import uuid
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db, migrate  # noqa: E402
from app.domains.documents.ingestion import (  # noqa: E402
    IngestRequest,
    accept_revision,
    claim_materialization,
    materialize_claimed_revision,
)
from app.domains.query import embedding_versions  # noqa: E402
from app.domains.query.vector_search import approximate_vector_search  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile fair concurrent preprocessing on durable revisions."
    )
    parser.add_argument("--workspaces", type=int, default=10)
    parser.add_argument("--documents-per-workspace", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--queries-per-workspace", type=int, default=3,
        help="tenant-scoped ANN requests issued while preprocessing is active",
    )
    parser.add_argument("--max-query-p95-ms", type=float, default=250.0)
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()
    if min(args.workspaces, args.documents_per_workspace, args.concurrency,
           args.queries_per_workspace) <= 0:
        parser.error("workspaces, documents, concurrency, and queries must be positive")
    if args.max_query_p95_ms <= 0:
        parser.error("--max-query-p95-ms must be positive")
    return args


def _p95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


async def main() -> int:
    args = parse_args()
    await migrate.run()
    pool = await db.get_pool()
    workspace_ids = []
    served = []
    query_latencies_ms = []
    served_lock = asyncio.Lock()
    total = args.workspaces * args.documents_per_workspace
    started = time.perf_counter()
    try:
        async with pool.acquire() as conn:
            for index in range(args.workspaces):
                workspace_ids.append(await conn.fetchval(
                    "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                    f"Worker load profile {index}",
                    f"worker-profile-{uuid.uuid4().hex[:16]}",
                ))
        for workspace_index, workspace_id in enumerate(workspace_ids):
            for document_index in range(args.documents_per_workspace):
                request = IngestRequest(
                    source="load-profile",
                    title=f"workspace {workspace_index} revision {document_index}",
                    text=(
                        f"Durable worker profile evidence for workspace {workspace_index}, "
                        f"document {document_index}."
                    ),
                    idempotency_key=f"worker-profile-{workspace_index}-{document_index}",
                )
                _, _, duplicate = await accept_revision(request, workspace_id)
                if duplicate:
                    raise RuntimeError("generated durable profile revision was unexpectedly duplicate")

        async def preprocess() -> None:
            while True:
                claimed = await claim_materialization()
                if claimed is None:
                    return
                if not await materialize_claimed_revision(claimed):
                    raise RuntimeError(f"materialization failed for revision {claimed.revision_id}")
                async with served_lock:
                    served.append(claimed.workspace_id)

        async def query_while_materializing() -> None:
            remaining = {workspace_id: args.queries_per_workspace for workspace_id in workspace_ids}
            while any(remaining.values()):
                progressed = False
                for workspace_id, needed in tuple(remaining.items()):
                    if needed <= 0:
                        continue
                    async with pool.acquire() as conn:
                        model_id = await embedding_versions.active_model(conn, workspace_id)
                        if model_id is None:
                            continue
                        seed = await conn.fetchrow(
                            "SELECT ce.embedding::text AS embedding FROM chunk_embeddings ce "
                            "JOIN chunks c ON c.id=ce.chunk_id JOIN documents d ON d.id=c.document_id "
                            "WHERE c.workspace_id=$1 AND d.workspace_id=$1 AND d.is_active "
                            "AND ce.embedding_model_id=$2 ORDER BY c.id LIMIT 1",
                            workspace_id, model_id,
                        )
                        if seed is None:
                            continue
                        started_query = time.perf_counter()
                        result = await approximate_vector_search(
                            conn, qvec=seed["embedding"], workspace_id=workspace_id,
                            embedding_model_id=model_id, limit=1,
                        )
                        result_ids = [row["id"] for row in result.rows]
                        owned_count = await conn.fetchval(
                            "SELECT count(*) FROM chunks WHERE workspace_id=$1 "
                            "AND id = ANY($2::int[])",
                            workspace_id, result_ids,
                        )
                    if not result.rows or owned_count != len(result_ids):
                        raise RuntimeError("tenant-scoped profile query returned invalid results")
                    query_latencies_ms.append((time.perf_counter() - started_query) * 1000)
                    remaining[workspace_id] -= 1
                    progressed = True
                if not progressed:
                    await asyncio.sleep(0.01)

        await asyncio.gather(
            *(preprocess() for _ in range(args.concurrency)),
            query_while_materializing(),
        )
        elapsed_s = time.perf_counter() - started
        counts = Counter(served)
        first_round = served[:args.workspaces]
        fair_first_round = set(first_round) == set(workspace_ids)
        min_served = min(counts.get(workspace_id, 0) for workspace_id in workspace_ids)
        max_served = max(counts.get(workspace_id, 0) for workspace_id in workspace_ids)
        query_p95 = _p95(query_latencies_ms)
        print(
            f"worker_load_profile workspaces={args.workspaces} documents={total} "
            f"concurrency={args.concurrency} elapsed_s={elapsed_s:.2f} "
            f"throughput_docs_s={total / elapsed_s:.2f} "
            f"query_p95_ms={query_p95:.2f} query_budget_ms={args.max_query_p95_ms:.2f} "
            f"first_round_fair={fair_first_round} served_range={min_served}-{max_served}"
        )
        return 0 if (
            len(served) == total and fair_first_round and min_served == max_served
            and len(query_latencies_ms) == args.workspaces * args.queries_per_workspace
            and query_p95 <= args.max_query_p95_ms
        ) else 1
    finally:
        if workspace_ids and not args.keep_workspaces:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM workspaces WHERE id = ANY($1::int[])", workspace_ids)
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
