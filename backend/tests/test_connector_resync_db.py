"""Periodic connector re-sync tick: which connections it enqueues, with what
job kind, and the active-job / interval / selection guards. The tick's dispatch
(asyncio background sync) is stubbed so no network call happens."""

import pytest

from app.domains.connectors import service


@pytest.fixture
def captured_dispatch(monkeypatch):
    """Replace _dispatch_sync with a recorder so no real sync task is spawned."""
    calls = []
    monkeypatch.setattr(service, "_dispatch_sync", lambda provider, job_id: calls.append((provider, job_id)))
    return calls


async def _make_connection(conn, workspace_id, provider, *, last_sync_at=None, status="connected", token="enc"):
    return await conn.fetchval(
        "INSERT INTO source_connections(workspace_id, provider, name, status, "
        "external_workspace_id, access_token_enc, last_sync_at) "
        "VALUES($1, $2, $3, $4, $5, $6, $7) RETURNING id",
        workspace_id, provider, f"{provider} conn", status,
        f"ext-{provider}", token, last_sync_at,
    )


async def _make_stream(conn, workspace_id, connection_id, provider, *, selected=True):
    return await conn.fetchval(
        "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, name, selected) "
        "VALUES($1, $2, $3, $4, $5, $6) RETURNING id",
        workspace_id, connection_id, provider, "proj-1", "Project One", selected,
    )


async def _jobs(conn, connection_id):
    return await conn.fetch(
        "SELECT id, kind, status, state FROM sync_jobs WHERE connection_id=$1", connection_id)


@pytest.mark.parametrize("provider", [
    "jira", "github", "linear", "confluence", "discord", "googledocs", "notion", "figma",
])
async def test_never_synced_connection_enqueues_backfill(pool, workspace_id, captured_dispatch, provider):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, provider, last_sync_at=None)
        await _make_stream(conn, workspace_id, cid, provider, selected=True)

    n = await service.resync_tick()

    async with pool.acquire() as conn:
        jobs = await _jobs(conn, cid)
    assert n >= 1
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "backfill"          # never synced -> full backfill
    assert dict(jobs[0]["state"]) == {}           # window left unset -> per-stream resolution
    assert (provider, jobs[0]["id"]) in captured_dispatch


async def test_already_synced_due_connection_enqueues_reconcile(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        # synced long ago -> due, but not "never" -> reconcile kind
        cid = await _make_connection(conn, workspace_id, "jira")
        await conn.execute(
            "UPDATE source_connections SET last_sync_at = now() - interval '30 days' WHERE id=$1", cid)
        await _make_stream(conn, workspace_id, cid, "jira", selected=True)

    await service.resync_tick()

    async with pool.acquire() as conn:
        jobs = await _jobs(conn, cid)
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "reconcile"


async def test_recently_synced_connection_is_not_due(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "jira")
        await conn.execute(
            "UPDATE source_connections SET last_sync_at = now() WHERE id=$1", cid)
        await _make_stream(conn, workspace_id, cid, "jira", selected=True)

    await service.resync_tick()

    async with pool.acquire() as conn:
        assert len(await _jobs(conn, cid)) == 0
    assert captured_dispatch == []


async def test_no_selected_streams_enqueues_nothing(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "github", last_sync_at=None)
        await _make_stream(conn, workspace_id, cid, "github", selected=False)

    await service.resync_tick()

    async with pool.acquire() as conn:
        assert len(await _jobs(conn, cid)) == 0


async def test_active_job_blocks_duplicate_enqueue(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "jira", last_sync_at=None)
        await _make_stream(conn, workspace_id, cid, "jira", selected=True)
        await conn.execute(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
            "VALUES($1, $2, 'jira', 'running', 'backfill', '{}'::jsonb, '{}'::jsonb)",
            workspace_id, cid)

    await service.resync_tick()

    async with pool.acquire() as conn:
        jobs = await _jobs(conn, cid)
    # only the pre-existing running job; the tick added none
    assert len(jobs) == 1
    assert jobs[0]["status"] == "running"
    assert captured_dispatch == []


async def test_stale_connector_job_is_requeued_and_dispatched(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "jira")
        stream_id = await _make_stream(conn, workspace_id, cid, "jira", selected=True)
        job_id = await conn.fetchval(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
            "VALUES($1, $2, 'jira', 'running', 'backfill', '{}'::jsonb, '{}'::jsonb) RETURNING id",
            workspace_id, cid,
        )
        await conn.execute(
            "UPDATE sync_jobs SET updated_at=now() - interval '2 hours' WHERE id=$1", job_id
        )
        await conn.execute(
            "UPDATE source_streams SET status='syncing' WHERE id=$1", stream_id
        )

    assert await service.recover_stuck_sync_jobs() == 1

    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT status, error FROM sync_jobs WHERE id=$1", job_id)
        stream = await conn.fetchrow("SELECT status, last_error FROM source_streams WHERE id=$1", stream_id)
    assert job["status"] == "pending"
    assert "abandoned" in job["error"]
    assert stream["status"] == "idle"
    assert "abandoned" in stream["last_error"]
    assert ("jira", job_id) in captured_dispatch


async def test_second_tick_does_not_double_enqueue(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "jira", last_sync_at=None)
        await _make_stream(conn, workspace_id, cid, "jira", selected=True)

    await service.resync_tick()           # creates a pending job
    await service.resync_tick()           # pending job is "active" -> guard blocks

    async with pool.acquire() as conn:
        assert len(await _jobs(conn, cid)) == 1


async def test_disconnected_connection_skipped(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "jira", last_sync_at=None, status="revoked")
        await _make_stream(conn, workspace_id, cid, "jira", selected=True)

    await service.resync_tick()

    async with pool.acquire() as conn:
        assert len(await _jobs(conn, cid)) == 0


# ---- onboarding fast-slice -> full backfill chaining ----

async def _complete_backfill(conn, workspace_id, cid, state):
    return await conn.fetchval(
        "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
        "VALUES($1, $2, 'slack', 'complete', 'backfill', $3, '{}'::jsonb) RETURNING id",
        workspace_id, cid, state,
    )


async def test_fast_slice_chains_full_backfill(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "slack")
        slice_id = await _complete_backfill(conn, workspace_id, cid, {"days": 7, "then_full_days": 90})

    await service._chain_full_backfill(slice_id)

    async with pool.acquire() as conn:
        new = [j for j in await _jobs(conn, cid) if j["status"] == "pending"]
    assert len(new) == 1
    assert dict(new[0]["state"]) == {"days": 90}        # full window, no further chain flag
    assert ("slack", new[0]["id"]) in captured_dispatch  # dispatched (real runner stubbed)


async def test_no_chain_without_then_full_flag(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "slack")
        await _complete_backfill(conn, workspace_id, cid, {"days": 90})  # ordinary manual sync

    await service._chain_full_backfill(
        (await _latest_job_id(pool, cid)))

    async with pool.acquire() as conn:
        assert len(await _jobs(conn, cid)) == 1          # nothing chained
    assert captured_dispatch == []


async def test_no_chain_when_slice_failed(pool, workspace_id, captured_dispatch):
    async with pool.acquire() as conn:
        cid = await _make_connection(conn, workspace_id, "slack")
        failed = await conn.fetchval(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, state, stats) "
            "VALUES($1, $2, 'slack', 'failed', 'backfill', $3, '{}'::jsonb) RETURNING id",
            workspace_id, cid, {"days": 7, "then_full_days": 90})

    await service._chain_full_backfill(failed)

    async with pool.acquire() as conn:
        pending = [j for j in await _jobs(conn, cid) if j["status"] == "pending"]
    assert pending == []                                 # a failed slice is retried, not deepened


async def _latest_job_id(pool, cid):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM sync_jobs WHERE connection_id=$1 ORDER BY id DESC LIMIT 1", cid)


# ---- workspace status gate fields ----

async def test_workspace_status_gate_fields(pool, workspace_id):
    from app.domains.auth.service import AuthContext
    from app.domains.workspace import service as ws

    ctx = AuthContext(
        user_id=1, email="o@x.co", display_name="O",
        workspace_id=workspace_id, workspace_name="WS", role="owner",
        session_id=1, workspaces=[],
    )

    s = await ws.workspace_status(ctx)
    assert s["has_workspace"] is True
    assert s["has_source"] is False and s["memory_ready"] is False

    async with pool.acquire() as conn:                   # connected source + selected stream
        cid = await _make_connection(conn, workspace_id, "slack")
        await _make_stream(conn, workspace_id, cid, "slack", selected=True)
    assert (await ws.workspace_status(ctx))["has_source"] is True

    async with pool.acquire() as conn:                   # a formed memory node
        await conn.execute(
            "INSERT INTO memory_nodes(workspace_id, kind, label) VALUES($1, 'decision', 'X')",
            workspace_id)
    assert (await ws.workspace_status(ctx))["memory_ready"] is True
