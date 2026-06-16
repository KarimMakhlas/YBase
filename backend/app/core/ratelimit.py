"""In-process sliding-window rate limiting for the expensive endpoints.

Per-user, in memory — one uvicorn process is the deployment story, so no
shared store needed. Login throttling is separate (auth_login_attempts)."""

import time
from collections import deque
from typing import Deque, Dict, Hashable

from fastapi import HTTPException

from . import config


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


query_limiter = SlidingWindowLimiter(config.QUERY_RATE_PER_MINUTE)
ingest_limiter = SlidingWindowLimiter(config.INGEST_RATE_PER_MINUTE)
# Keyed by client IP (not user) — these guard the unauthenticated auth routes.
auth_limiter = SlidingWindowLimiter(config.AUTH_RATE_PER_MINUTE)
