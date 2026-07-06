"""Plan-tier daily formation quotas: claim-time enforcement, parking as
'rate_limited', janitor requeue after the UTC-day rollover, and audit."""

from app.core import config
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker


def _req(**over):
    base = dict(source="meeting", title="Quota test", text="Some decision content.")
    base.update(over)
    return IngestRequest(**base)


async def _seed_success_run(pool, workspace_id, n=1):
    async with pool.acquire() as conn:
        for _ in range(n):
            await conn.execute(
                "INSERT INTO formation_runs(workspace_id, status, started_at, finished_at) "
                "VALUES($1, 'success', now(), now())",
                workspace_id,
            )


async def test_over_quota_parks_docs_until_tomorrow(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TRIAL", 1)
    await _seed_success_run(pool, workspace_id)  # today's quota is spent
    doc_a, _ = await ingest_document(_req(), workspace_id=workspace_id)
    doc_b, _ = await ingest_document(_req(text="More content."), workspace_id=workspace_id)

    assert await worker._claim() is None  # claim consumed by the quota check

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, formation_status, formation_next_attempt_at FROM documents "
            "WHERE workspace_id=$1 ORDER BY id", workspace_id)
        audit = await conn.fetchrow(
            "SELECT action, data FROM audit_events "
            "WHERE workspace_id=$1 AND action='formation_rate_limited'", workspace_id)
    assert {r["id"] for r in rows} == {doc_a, doc_b}
    for r in rows:
        assert r["formation_status"] == "rate_limited"
        assert r["formation_next_attempt_at"] is not None  # tomorrow 00:00 UTC
    assert audit is not None
    assert audit["data"]["quota"] == 1
    assert audit["data"]["count"] == 2

    # still nothing claimable
    assert await worker._claim() is None


async def test_janitor_requeues_after_window_rollover(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TRIAL", 1)
    await _seed_success_run(pool, workspace_id)
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    assert await worker._claim() is None  # parked

    async with pool.acquire() as conn:  # fast-forward to "tomorrow"
        await conn.execute(
            "UPDATE documents SET formation_next_attempt_at = now() - interval '1 minute' "
            "WHERE id=$1", doc_id)
        # yesterday's success no longer counts against today
        await conn.execute(
            "UPDATE formation_runs SET started_at = now() - interval '25 hours'")
    await worker.janitor_tick()

    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id) == "pending"
    claimed = await worker._claim()
    assert claimed is not None and claimed.doc_id == doc_id


async def test_quota_zero_means_unlimited(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TRIAL", 0)
    await _seed_success_run(pool, workspace_id, n=50)
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    assert claimed is not None and claimed.doc_id == doc_id


async def test_team_plan_uses_team_quota(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TRIAL", 1)
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TEAM", 10)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE workspaces SET plan='team' WHERE id=$1", workspace_id)
    try:
        await _seed_success_run(pool, workspace_id)  # would exhaust trial's 1
        doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
        claimed = await worker._claim()
        assert claimed is not None and claimed.doc_id == doc_id  # team allows 10
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE workspaces SET plan='trial' WHERE id=$1", workspace_id)


async def test_quota_check_failure_releases_doc_and_lock(pool, workspace_id, monkeypatch):
    """A DB error during the quota check must not strand the claimed doc in
    'processing' — it returns to pending and the workspace stays claimable."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)

    async def _boom(_ws):
        raise RuntimeError("quota check DB error")

    monkeypatch.setattr(worker, "_enforce_quota", _boom)
    try:
        await worker._claim()
        assert False, "expected the quota error to propagate"
    except RuntimeError:
        pass
    async with pool.acquire() as conn:
        status = await conn.fetchval(
            "SELECT formation_status FROM documents WHERE id=$1", doc_id)
    assert status == "pending"  # released, not stranded in 'processing'

    monkeypatch.undo()  # restore real quota check
    claimed = await worker._claim()  # workspace claimable again
    assert claimed is not None and claimed.doc_id == doc_id


async def test_unknown_plan_is_unlimited(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(config, "FORMATION_DAILY_QUOTA_TRIAL", 1)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE workspaces SET plan='enterprise-custom' WHERE id=$1", workspace_id)
    try:
        await _seed_success_run(pool, workspace_id, n=5)
        doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
        claimed = await worker._claim()
        assert claimed is not None and claimed.doc_id == doc_id
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE workspaces SET plan='trial' WHERE id=$1", workspace_id)
