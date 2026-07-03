"""Graph primitives against a real Postgres: atomic upsert merge, edge dedup,
bounded expansion, and duplicate-node folding."""

from app.domains.memory import graph


async def test_upsert_merges_on_label(pool, workspace_id):
    async with pool.acquire() as conn:
        a = await graph.upsert_node(conn, workspace_id, "decision", "Use Postgres",
                                    summary="short", status="proposed", data={"date": "2026-01-01"})
        b = await graph.upsert_node(conn, workspace_id, "decision", "use postgres",
                                    summary="a much longer summary wins", status="decided",
                                    data={"made_by": ["Maya"]})
        assert a == b
        row = await conn.fetchrow("SELECT summary, status, data FROM memory_nodes WHERE id=$1", a)
        assert row["summary"] == "a much longer summary wins"
        assert row["status"] == "decided"
        assert row["data"]["date"] == "2026-01-01"          # old keys survive
        assert row["data"]["made_by"] == ["Maya"]           # new keys merge in


async def test_upsert_status_none_keeps_existing(pool, workspace_id):
    async with pool.acquire() as conn:
        a = await graph.upsert_node(conn, workspace_id, "question", "Cache warming?",
                                    status="open")
        await graph.upsert_node(conn, workspace_id, "question", "Cache warming?")
        status = await conn.fetchval("SELECT status FROM memory_nodes WHERE id=$1", a)
        assert status == "open"


async def test_add_edge_dedups_and_skips_self(pool, workspace_id):
    async with pool.acquire() as conn:
        a = await graph.upsert_node(conn, workspace_id, "decision", "A")
        b = await graph.upsert_node(conn, workspace_id, "topic", "db")
        await graph.add_edge(conn, workspace_id, a, b, "about")
        await graph.add_edge(conn, workspace_id, a, b, "about")
        await graph.add_edge(conn, workspace_id, a, a, "about")
        n = await conn.fetchval("SELECT count(*) FROM memory_edges")
        assert n == 1


async def test_expand_respects_hops(pool, workspace_id):
    async with pool.acquire() as conn:
        ids = [await graph.upsert_node(conn, workspace_id, "decision", f"D{i}") for i in range(4)]
        # chain D0 - D1 - D2 - D3
        for i in range(3):
            await graph.add_edge(conn, workspace_id, ids[i], ids[i + 1], "relates_to")
        one_hop, _ = await graph.expand(conn, workspace_id, [ids[0]], hops=1)
        assert one_hop == {ids[0], ids[1]}
        two_hop, _ = await graph.expand(conn, workspace_id, [ids[0]], hops=2)
        assert two_hop == {ids[0], ids[1], ids[2]}


async def test_merge_nodes_moves_evidence_and_edges(pool, workspace_id):
    async with pool.acquire() as conn:
        doc = await conn.fetchval(
            "INSERT INTO documents(workspace_id, source, title, raw_text) "
            "VALUES($1, 'slack', 't', 'x') RETURNING id", workspace_id)
        chunk = await conn.fetchval(
            "INSERT INTO chunks(document_id, chunk_index, text, embedding) "
            "VALUES($1, 0, 'x', $2::vector) RETURNING id",
            doc, "[" + ",".join(["0"] * 512) + "]")
        keep = await graph.upsert_node(conn, workspace_id, "decision", "Choose Postgres",
                                       summary="original")
        drop = await graph.upsert_node(conn, workspace_id, "decision", "Postgres selection",
                                       summary="a longer duplicate summary")
        topic = await graph.upsert_node(conn, workspace_id, "topic", "database")
        await graph.add_edge(conn, workspace_id, drop, topic, "about")
        await graph.link_chunk(conn, chunk, drop)

        await graph.merge_nodes(conn, keep, drop)

        assert await conn.fetchval("SELECT count(*) FROM memory_nodes WHERE id=$1", drop) == 0
        # evidence and edges now point at the survivor
        assert await conn.fetchval(
            "SELECT count(*) FROM chunk_links WHERE node_id=$1 AND chunk_id=$2", keep, chunk) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM memory_edges WHERE src=$1 AND dst=$2 AND relation='about'",
            keep, topic) == 1
        summary = await conn.fetchval("SELECT summary FROM memory_nodes WHERE id=$1", keep)
        assert summary == "a longer duplicate summary"


async def test_consolidation_merges_incrementally_and_stores_embeddings(pool, workspace_id):
    """merge_similar_decisions embeds only the touched decisions (plus
    never-embedded legacy rows), stores the vectors on the nodes, merges the
    near-duplicate, and refreshes the kept node's signature after the merge."""
    from app.domains.memory import consolidate

    async with pool.acquire() as conn:
        a = await graph.upsert_node(conn, workspace_id, "decision",
                                    "Use PostgreSQL as the primary database for v1",
                                    summary="relational fits the workload")
        b = await graph.upsert_node(conn, workspace_id, "decision",
                                    "Use PostgreSQL as primary database for v1",
                                    summary="relational fits the workload")
        c = await graph.upsert_node(conn, workspace_id, "decision",
                                    "Adopt Redis cache for dashboard aggregates",
                                    summary="precomputed aggregates are hot")

    merged = await consolidate.merge_similar_decisions(workspace_id, touched_ids=[b])
    assert [(m["kept"], m["dropped"]) for m in merged] == [(a, b)]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, embedding IS NOT NULL AS has_vec FROM memory_nodes "
            "WHERE kind='decision' ORDER BY id")
    has_vec = {r["id"]: r["has_vec"] for r in rows}
    assert b not in has_vec              # duplicate deleted
    assert has_vec == {a: True, c: True}  # survivor re-embedded, legacy row backfilled

    # second run with nothing touched: everything already embedded → no merges
    assert await consolidate.merge_similar_decisions(workspace_id, touched_ids=[]) == []
