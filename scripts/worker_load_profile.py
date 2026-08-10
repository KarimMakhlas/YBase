#!/usr/bin/env python3
"""Exercise durable acceptance, fair claims, and concurrent preprocessing.

Run against staging before changing worker concurrency or Neon connection
capacity. It creates isolated workspaces and removes them automatically.
"""

import argparse
import asyncio
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile fair concurrent preprocessing on durable revisions."
    )
    parser.add_argument("--workspaces", type=int, default=10)
    parser.add_argument("--documents-per-workspace", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()
    if min(args.workspaces, args.documents_per_workspace, args.concurrency) <= 0:
        parser.error("--workspaces, --documents-per-workspace, and --concurrency must be positive")
    return args


async def main() -> int:
    args = parse_args()
    await migrate.run()
    pool = await db.get_pool()
    workspace_ids = []
    served = []
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

        await asyncio.gather(*(preprocess() for _ in range(args.concurrency)))
        elapsed_s = time.perf_counter() - started
        counts = Counter(served)
        first_round = served[:args.workspaces]
        fair_first_round = set(first_round) == set(workspace_ids)
        min_served = min(counts.get(workspace_id, 0) for workspace_id in workspace_ids)
        max_served = max(counts.get(workspace_id, 0) for workspace_id in workspace_ids)
        print(
            f"worker_load_profile workspaces={args.workspaces} documents={total} "
            f"concurrency={args.concurrency} elapsed_s={elapsed_s:.2f} "
            f"throughput_docs_s={total / elapsed_s:.2f} "
            f"first_round_fair={fair_first_round} served_range={min_served}-{max_served}"
        )
        return 0 if len(served) == total and fair_first_round and min_served == max_served else 1
    finally:
        if workspace_ids and not args.keep_workspaces:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM workspaces WHERE id = ANY($1::int[])", workspace_ids)
        await db.close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
