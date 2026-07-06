"""Cross-instance coordination via Redis — optional, degrades gracefully.

Postgres remains the source of truth for all job state; Redis carries only
ephemeral coordination: per-workspace formation locks, cross-instance wake
signals, leader election for the periodic tickers, worker heartbeats, and
shared rate-limit counters. Every function is a safe no-op / permissive
default when REDIS_URL is unset, which reproduces the original
single-instance behavior exactly. Losing Redis mid-flight degrades to
"correct but single-instance-safe" — never to data loss: a missed lock at
worst lets two instances form the same workspace concurrently, which the
(kind, lower(label)) node upsert and consolidation absorb.
"""

import asyncio
import logging
import secrets
import time
from typing import Awaitable, Callable, Hashable, Iterable, Optional, Set

from fastapi import HTTPException

from . import config

log = logging.getLogger("ybase.coordination")

_client = None  # redis.asyncio.Redis — lazy singleton, mirrors db._pool

# Atomic compare-and-delete: only the holder of the token may release. Without
# this, a slow instance whose lock already expired could delete the next
# holder's lock.
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def enabled() -> bool:
    return bool(config.REDIS_URL)


def _key(*parts: object) -> str:
    return ":".join([config.REDIS_KEY_PREFIX, *[str(p) for p in parts]])


def get_client():
    global _client
    if _client is None:
        import redis.asyncio as redis  # lazy: only needed when REDIS_URL is set

        _client = redis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


async def status() -> str:
    """'disabled' | 'ok' | 'error' — for /api/health/details."""
    if not enabled():
        return "disabled"
    try:
        await get_client().ping()
        return "ok"
    except Exception:
        return "error"


# ── Per-workspace formation lock ─────────────────────────────────────────────

# Sentinel returned when Redis is disabled or unreachable: the caller proceeds
# exactly as before Redis existed (in-process claim serialization is then the
# whole correctness story), and release becomes a no-op.
LOCAL_TOKEN = "local"


def _lock_ttl_ms() -> int:
    # Auto TTL must outlive whichever job the lock guards: both formation
    # (_run_one, FORMATION_TASK_TIMEOUT_S) and batch consolidation
    # (_run_consolidation, CONSOLIDATION_TASK_TIMEOUT_S) hold this workspace
    # lock and are each hard-bounded by asyncio.wait_for, so max(...)+60s can
    # only expire after the running job is dead (crash/OOM) — no refresh loop.
    auto = max(config.FORMATION_TASK_TIMEOUT_S, config.CONSOLIDATION_TASK_TIMEOUT_S) + 60
    ttl_s = config.FORMATION_LOCK_TTL_S or auto
    return max(1000, int(ttl_s * 1000))


async def try_workspace_lock(workspace_id: int) -> Optional[str]:
    """Acquire the cross-instance formation lock for a workspace. Returns a
    token for release_workspace_lock, or None when another instance holds it.
    Fails open (returns LOCAL_TOKEN) when Redis is disabled or erroring."""
    if not enabled():
        return LOCAL_TOKEN
    token = secrets.token_hex(16)
    try:
        ok = await get_client().set(
            _key("wslock", workspace_id), token, nx=True, px=_lock_ttl_ms()
        )
        return token if ok else None
    except Exception:
        log.warning("redis lock acquire failed — degrading to single-instance mode")
        return LOCAL_TOKEN


async def release_workspace_lock(workspace_id: int, token: Optional[str]) -> None:
    if not enabled() or not token or token == LOCAL_TOKEN:
        return
    try:
        await get_client().eval(_RELEASE_LUA, 1, _key("wslock", workspace_id), token)
    except Exception:
        log.warning("redis lock release failed — TTL will expire it")


async def locked_workspaces(workspace_ids: Iterable[int]) -> Set[int]:
    """Subset of workspace_ids whose formation lock is held by any instance.
    Empty when Redis is disabled or unreachable (treat all as unlocked —
    matches pre-Redis recovery behavior)."""
    ids = list(workspace_ids)
    if not enabled() or not ids:
        return set()
    try:
        vals = await get_client().mget([_key("wslock", w) for w in ids])
        return {w for w, v in zip(ids, vals) if v is not None}
    except Exception:
        log.warning("redis MGET failed — treating all workspaces as unlocked")
        return set()


# ── Formation wake signal ────────────────────────────────────────────────────


async def publish_wake() -> None:
    """Best-effort cross-instance wake after an enqueue. Lost messages are
    fine: the worker loop's 15s poll is the backstop."""
    if not enabled():
        return
    try:
        await get_client().publish(_key("wake", "formation"), "1")
    except Exception:
        pass


def subscribe_wake(callback: Callable[[], None]) -> asyncio.Task:
    """Spawn a task invoking callback() on every wake message, reconnecting
    with backoff on connection loss. Returns the task (cancel to stop); a
    completed no-op task when Redis is disabled."""

    async def _noop() -> None:
        return None

    if not enabled():
        return asyncio.create_task(_noop())

    async def _listen() -> None:
        while True:
            pubsub = None
            try:
                pubsub = get_client().pubsub()
                await pubsub.subscribe(_key("wake", "formation"))
                async for msg in pubsub.listen():
                    if msg.get("type") == "message":
                        callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("wake subscriber lost connection; retrying in 5s")
                await asyncio.sleep(5)
            finally:
                # each retry allocates a fresh PubSub — close the old one so
                # reconnect loops don't accumulate pool connections
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass

    return asyncio.create_task(_listen())


# ── Ticker leader election ───────────────────────────────────────────────────


async def is_leader() -> bool:
    """Acquire-or-refresh the ticker leadership lease. Unconditionally True
    when Redis is disabled (a single instance is trivially the leader) and on
    Redis errors (duplicate ticks beat no ticks — every tick is idempotent)."""
    if not enabled():
        return True
    key = _key("leader", "ticker")
    me = config.WORKER_INSTANCE_ID
    try:
        client = get_client()
        if await client.set(key, me, nx=True, ex=config.LEADER_TTL_S):
            return True
        if await client.get(key) == me:
            await client.expire(key, config.LEADER_TTL_S)
            return True
        return False
    except Exception:
        log.warning("redis leader check failed — assuming leadership")
        return True


async def resign_leader() -> None:
    """Give up the lease on shutdown so the next instance takes over
    immediately instead of waiting out LEADER_TTL_S."""
    if not enabled():
        return
    try:
        await get_client().eval(
            _RELEASE_LUA, 1, _key("leader", "ticker"), config.WORKER_INSTANCE_ID
        )
    except Exception:
        pass


# ── Worker heartbeats ────────────────────────────────────────────────────────

_HEARTBEAT_TTL_S = 30


async def heartbeat(workers: int) -> None:
    """Record this instance's live worker count so formation_health can report
    the whole fleet, not just the instance answering the request."""
    if not enabled():
        return
    try:
        await get_client().set(
            _key("hb", config.WORKER_INSTANCE_ID), str(workers), ex=_HEARTBEAT_TTL_S
        )
    except Exception:
        pass


async def fleet_workers() -> Optional[int]:
    """Sum of live worker counts across instances; None when Redis is
    disabled/unreachable (caller falls back to its local count)."""
    if not enabled():
        return None
    try:
        client = get_client()
        keys = [k async for k in client.scan_iter(match=_key("hb", "*"))]
        if not keys:
            return 0
        vals = await client.mget(keys)
        return sum(int(v) for v in vals if v)
    except Exception:
        return None


# ── Shared rate limiting ─────────────────────────────────────────────────────


class RedisWindowLimiter:
    """Fixed-window rate limiter sharing counts across instances via
    INCR+EXPIRE. Async twin of ratelimit.SlidingWindowLimiter; fails open on
    Redis errors (availability over strictness for API limits)."""

    def __init__(self, name: str, max_events: int, window_s: float = 60.0) -> None:
        self.name = name
        self.max_events = max_events
        self.window_s = window_s

    async def allow(self, key: Hashable) -> bool:
        if self.max_events <= 0:  # 0 disables the limit
            return True
        bucket = int(time.time() // self.window_s)
        rkey = _key("rl", self.name, key, bucket)
        try:
            client = get_client()
            n = await client.incr(rkey)
            if n == 1:
                # 2x window: the key must survive its own window plus clock skew.
                await client.expire(rkey, int(self.window_s * 2))
            return n <= self.max_events
        except Exception:
            log.warning("redis rate limit failed open for %s", self.name)
            return True

    async def enforce(self, key: Hashable, what: str) -> None:
        if not await self.allow(key):
            raise HTTPException(
                429, f"{what} rate limit exceeded — try again in a minute"
            )
