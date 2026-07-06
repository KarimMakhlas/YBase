"""Coordination-layer tests. The no-Redis tests always run (REDIS_URL is
forced empty in conftest); the Redis-backed ones use the redis_coord fixture
and self-skip when localhost:6380 is down."""

import asyncio

import pytest
from fastapi import HTTPException

from app.core import config, coordination


# ── Disabled mode: every function is a safe no-op / permissive default ──────


async def test_disabled_lock_is_permissive():
    assert not coordination.enabled()
    token = await coordination.try_workspace_lock(1)
    assert token == coordination.LOCAL_TOKEN
    await coordination.release_workspace_lock(1, token)  # no-op, no error


async def test_disabled_helpers_default_open():
    assert await coordination.locked_workspaces([1, 2, 3]) == set()
    assert await coordination.is_leader() is True
    assert await coordination.fleet_workers() is None
    assert await coordination.status() == "disabled"
    await coordination.publish_wake()  # no-op
    await coordination.heartbeat(3)  # no-op
    task = coordination.subscribe_wake(lambda: None)
    await task  # completed no-op task


# ── Workspace lock ───────────────────────────────────────────────────────────


async def test_lock_acquire_conflict_release(redis_coord):
    token = await redis_coord.try_workspace_lock(42)
    assert token and token != redis_coord.LOCAL_TOKEN
    assert await redis_coord.try_workspace_lock(42) is None  # held
    assert await redis_coord.locked_workspaces([42, 43]) == {42}
    await redis_coord.release_workspace_lock(42, token)
    assert await redis_coord.try_workspace_lock(42) is not None  # free again


async def test_lock_wrong_token_release_is_noop(redis_coord):
    token = await redis_coord.try_workspace_lock(7)
    assert token
    await redis_coord.release_workspace_lock(7, "not-the-token")
    assert await redis_coord.try_workspace_lock(7) is None  # still held


async def test_lock_ttl_expires(redis_coord, monkeypatch):
    # _lock_ttl_ms clamps to a 1s floor, so this is the shortest testable TTL.
    monkeypatch.setattr(config, "FORMATION_LOCK_TTL_S", 1.0)
    assert await redis_coord.try_workspace_lock(9)
    assert await redis_coord.try_workspace_lock(9) is None
    await asyncio.sleep(1.15)
    assert await redis_coord.try_workspace_lock(9) is not None  # expired


# ── Leader election ──────────────────────────────────────────────────────────


async def test_leader_single_winner_and_handoff(redis_coord, monkeypatch):
    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "instance-a")
    assert await redis_coord.is_leader() is True
    assert await redis_coord.is_leader() is True  # refresh, still leader

    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "instance-b")
    assert await redis_coord.is_leader() is False  # a holds the lease

    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "instance-a")
    await redis_coord.resign_leader()
    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "instance-b")
    assert await redis_coord.is_leader() is True  # immediate takeover


# ── Wake pub/sub ─────────────────────────────────────────────────────────────


async def test_wake_pubsub_delivers(redis_coord):
    woke = asyncio.Event()
    task = redis_coord.subscribe_wake(woke.set)
    try:
        # Subscription races the publish; retry until the message lands.
        for _ in range(20):
            await redis_coord.publish_wake()
            try:
                await asyncio.wait_for(woke.wait(), timeout=0.25)
                break
            except asyncio.TimeoutError:
                continue
        assert woke.is_set()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── Heartbeats ───────────────────────────────────────────────────────────────


async def test_heartbeats_sum_across_instances(redis_coord, monkeypatch):
    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "hb-a")
    await redis_coord.heartbeat(3)
    monkeypatch.setattr(config, "WORKER_INSTANCE_ID", "hb-b")
    await redis_coord.heartbeat(2)
    assert await redis_coord.fleet_workers() == 5


# ── Shared rate limiter ──────────────────────────────────────────────────────


async def test_redis_window_limiter_blocks_then_isolates(redis_coord):
    lim = coordination.RedisWindowLimiter("test", max_events=3)
    for _ in range(3):
        assert await lim.allow("user-1") is True
    assert await lim.allow("user-1") is False  # over budget
    assert await lim.allow("user-2") is True  # other keys unaffected
    with pytest.raises(HTTPException) as exc:
        await lim.enforce("user-1", "test")
    assert exc.value.status_code == 429


async def test_redis_window_limiter_zero_disables(redis_coord):
    lim = coordination.RedisWindowLimiter("off", max_events=0)
    for _ in range(10):
        assert await lim.allow("anyone") is True
