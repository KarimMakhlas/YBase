#!/usr/bin/env python3
"""Stage and atomically activate a workspace embedding version.

Switch the embedding provider/model in configuration first, then stage and
validate vectors without changing live reads:

    backend/.venv/bin/python scripts/reembed.py --workspace default --activate

Rollback never invokes an embedding provider:

    backend/.venv/bin/python scripts/reembed.py --workspace default \
        --rollback-to voyage:voyage-3-lite:512
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.core import db  # noqa: E402
from app.domains.query import embedding_versions  # noqa: E402
from app.providers.embeddings import active_embed_model, embed_texts, to_pgvector  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage or activate versioned workspace embeddings.")
    parser.add_argument("--workspace", required=True, help="workspace slug")
    parser.add_argument(
        "--model", default=None,
        help="expected active-provider model key (default: active provider model)",
    )
    parser.add_argument("--activate", action="store_true", help="activate only after complete coverage")
    parser.add_argument(
        "--rollback-to", default=None,
        help="activate an already-covered model key without embedding calls",
    )
    parser.add_argument("--batch", type=int, default=32, help="texts per embedding request")
    args = parser.parse_args()
    if args.batch <= 0:
        parser.error("--batch must be positive")
    if args.rollback_to and (args.activate or args.model):
        parser.error("--rollback-to cannot be combined with --activate or --model")
    return args


async def _workspace(conn, slug: str):
    row = await conn.fetchrow(
        "SELECT id, slug FROM workspaces WHERE lower(slug)=lower($1)", slug
    )
    if row is None:
        raise ValueError(f"workspace not found: {slug}")
    return row


async def _rollback(conn, workspace_id: int, model_key: str) -> int:
    model_id = await conn.fetchval(
        "SELECT id FROM embedding_models WHERE model_key=$1", model_key
    )
    if model_id is None:
        raise ValueError(f"embedding model not found: {model_key}")
    await embedding_versions.activate_model(conn, workspace_id, model_id)
    return model_id


async def main() -> int:
    args = parse_args()
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            workspace = await _workspace(conn, args.workspace)
            if args.rollback_to:
                model_id = await _rollback(conn, workspace["id"], args.rollback_to)
                print(f"rolled back workspace={workspace['slug']} active_model={args.rollback_to} id={model_id}")
                return 0

        active_key = await active_embed_model()
        model_key = args.model or active_key
        if model_key != active_key:
            raise ValueError(
                f"--model {model_key} does not match active provider model {active_key}; "
                "change provider configuration before staging"
            )
        async with pool.acquire() as conn:
            workspace = await _workspace(conn, args.workspace)
            model_id = await embedding_versions.ensure_model(conn, model_key)
            rows = await conn.fetch(
                "SELECT c.id, c.text FROM chunks c JOIN documents d ON d.id=c.document_id "
                "WHERE c.workspace_id=$1 AND d.workspace_id=$1 AND d.is_active "
                "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings ce "
                "                WHERE ce.chunk_id=c.id AND ce.embedding_model_id=$2) "
                "ORDER BY c.id",
                workspace["id"], model_id,
            )
        print(f"staging workspace={workspace['slug']} chunks={len(rows)} model={model_key}")
        for start in range(0, len(rows), args.batch):
            batch = rows[start:start + args.batch]
            vecs = await embed_texts([row["text"] for row in batch], kind="document")
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for row, vector in zip(batch, vecs):
                        await conn.execute(
                            "INSERT INTO chunk_embeddings(workspace_id, chunk_id, embedding_model_id, embedding) "
                            "VALUES($1,$2,$3,$4::vector) ON CONFLICT DO NOTHING",
                            workspace["id"], row["id"], model_id, to_pgvector(vector),
                        )
            print(f"  staged={min(start + args.batch, len(rows))}/{len(rows)}")

        async with pool.acquire() as conn:
            current = await embedding_versions.coverage(conn, workspace["id"], model_id)
            print(
                f"coverage workspace={workspace['slug']} model={model_key} "
                f"chunks={current.embedded_chunks}/{current.active_chunks}"
            )
            if args.activate:
                await embedding_versions.activate_model(conn, workspace["id"], model_id)
                print(f"activated workspace={workspace['slug']} model={model_key}")
        return 0
    finally:
        await db.close_pool()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
