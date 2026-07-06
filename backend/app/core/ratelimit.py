"""Rate limiting for the expensive endpoints.

Single instance: per-process sliding window (exact, in memory). Multi-instance
(REDIS_URL set): shared fixed-window counters in Redis, so N instances don't
silently multiply every budget by N. The module-level limiters are async
facades that pick the backend per call; call sites await them. Login
throttling is separate (auth_login_attempts)."""

import time
from collections import deque
from typing import Deque, Dict, Hashable

from fastapi import HTTPException

from . import config, coordination


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_s: float = 60.0) -> None:
        self.max_events = max_events
        self.window_s = window_s
        self._events: Dict[Hashable, Deque[float]] = {}

    def allow(self, key: Hashable) -> bool:
        if self.max_events <= 0:  # 0 disables the limit
            return True
        now = time.monotonic()
        dq = self._events.get(key)
        if dq is None:
            dq = self._events[key] = deque()
        while dq and now - dq[0] > self.window_s:
            dq.popleft()
        if len(dq) >= self.max_events:
            return False
        dq.append(now)
        return True

    def enforce(self, key: Hashable, what: str) -> None:
        if not self.allow(key):
            raise HTTPException(
                429, f"{what} rate limit exceeded — try again in a minute"
            )


class Limiter:
    """Async facade over the in-process sliding window (single instance) or
    the shared Redis fixed window (multi-instance). Exposes max_events and
    _events like SlidingWindowLimiter so tests can tune limits and reset
    local state."""

    def __init__(self, name: str, max_events: int, window_s: float = 60.0) -> None:
        self.name = name
        self._local = SlidingWindowLimiter(max_events, window_s)

    @property
    def max_events(self) -> int:
        return self._local.max_events

    @max_events.setter
    def max_events(self, value: int) -> None:
        self._local.max_events = value

    @property
    def _events(self) -> Dict[Hashable, Deque[float]]:
        return self._local._events

    async def allow(self, key: Hashable) -> bool:
        if coordination.enabled():
            return await coordination.RedisWindowLimiter(
                self.name, self.max_events, self._local.window_s
            ).allow(key)
        return self._local.allow(key)

    async def enforce(self, key: Hashable, what: str) -> None:
        if not await self.allow(key):
            raise HTTPException(
                429, f"{what} rate limit exceeded — try again in a minute"
            )


query_limiter = Limiter("query", config.QUERY_RATE_PER_MINUTE)
ingest_limiter = Limiter("ingest", config.INGEST_RATE_PER_MINUTE)
# Keyed by (workspace_id, api_key_id) — guards the machine-facing agent API.
agent_limiter = Limiter("agent", config.AGENT_RATE_PER_MINUTE)
# Keyed by client IP (not user) — these guard the unauthenticated auth routes.
auth_limiter = Limiter("auth", config.AUTH_RATE_PER_MINUTE)
# Keyed by Slack team id — guards the inbound Events webhook. Callers use
# .allow() and drop over-budget events (HTTP 200), never 429: a 429 just makes
# Slack retry and amplifies the flood.
slack_events_limiter = Limiter("slack_events", config.SLACK_EVENTS_RATE_PER_MINUTE)
