"""SLO tracking: formation_runs rows per terminal outcome, health counters,
retention pruning, and the /api/ops/slo aggregates. Uses the fake_llm fixture
— the first end-to-end formation coverage without a real provider."""

import asyncio

from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker
from app.providers import llm

from test_api_endpoints import _auth_client


def _req(**over):
    base = dict(source="meeting", title="Arch sync",
                text="We picked Postgres.\n\nBecause transactions.")
    base.update(over)
    return IngestRequest(**base)


async def test_success_run_recorded(pool, workspace_id, fake_llm):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT * FROM formation_runs WHERE document_id=$1", doc_id)
        doc_status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id)
    assert doc_status == "complete"
    assert run["status"] == "success"
    assert run["workspace_id"] == workspace_id
    assert run["attempt"] == 1
    assert run["queue_wait_ms"] is not None and run["queue_wait_ms"] >= 0
    assert run["duration_ms"] is not None and run["duration_ms"] >= 0
    assert run["finished_at"] is not None
    for stage in ("fetch", "llm", "persist", "consolidation"):
        assert stage in run["stage_timings"], f"missing stage {stage}"
    assert run["llm_provider"]


async def test_failed_run_recorded_and_backoff_intact(pool, workspace_id, monkeypatch):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)

    async def _boom(system, user, schema, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(llm, "structured_call", _boom)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT * FROM formation_runs WHERE document_id=$1", doc_id)
        doc = await conn.fetchrow(
            "SELECT formation_status, formation_attempts, formation_next_attempt_at "
            "FROM documents WHERE id=$1", doc_id)
    assert run["status"] == "failed"
    assert "provider exploded" in run["error"]
    # the pre-existing retry machinery is untouched
    assert doc["formation_status"] == "pending"
    assert doc["formation_attempts"] == 1
    assert doc["formation_next_attempt_at"] is not None


async def test_timeout_run_recorded(pool, workspace_id, monkeypatch):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    await worker._claim()

    async def _hang(_doc_id, _timer=None):
        await asyncio.sleep(30)

    monkeypatch.setattr("app.domains.memory.formation.run_formation", _hang)
    monkeypatch.setattr(config, "FORMATION_TASK_TIMEOUT_S", 0.2)
    await asyncio.wait_for(worker._run_one(doc_id), timeout=5)

    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT status, error FROM formation_runs WHERE document_id=$1", doc_id)
    assert run["status"] == "timeout"
    assert "timed out" in run["error"]


async def test_health_counts_recent_outcomes(pool, workspace_id, fake_llm):
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)
    health = await worker.formation_health()
    assert health["completed_1h"] == 1
    assert health["failed_1h"] == 0
    assert health["last_success_age_s"] is not None


async def test_prune_metrics_respects_retention(pool, workspace_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO formation_runs(workspace_id, status, started_at, finished_at) "
            "VALUES ($1, 'success', now() - interval '90 days', now() - interval '90 days'), "
            "       ($1, 'success', now(), now())",
            workspace_id,
        )
    await worker._prune_metrics()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM formation_runs") == 1


async def test_slo_endpoint_percentiles(pool, workspace_id):
    async with pool.acquire() as conn:
        for i in range(1, 11):  # durations 100..1000, queue waits 10..100
            await conn.execute(
                "INSERT INTO formation_runs(workspace_id, status, queue_wait_ms, "
                "duration_ms, stage_timings, started_at, finished_at) "
                "VALUES($1, $2, $3, $4, $5, now(), now())",
                workspace_id, "success" if i <= 9 else "failed",
                i * 10, i * 100, {"llm": i * 50},
            )
    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.get("/api/ops/slo?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runs"] == 10
    assert body["successes"] == 9
    assert body["failures"] == 1
    assert body["p50_ms"] == 550        # percentile_cont over 100..1000
    assert body["p95_ms"] == 955
    assert body["p95_queue_wait_ms"] == 95
    assert body["p95_llm_ms"] == 478    # over 50..500
    assert len(body["per_day"]) == 1
    assert body["per_day"][0]["runs"] == 10
    assert "queue" in body


async def test_slo_endpoint_requires_admin(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="member")
    async with client:
        resp = await client.get("/api/ops/slo")
    assert resp.status_code == 403
