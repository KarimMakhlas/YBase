"""Structured logging: every log line carries a request id, every HTTP request
logs method/path/status/duration, and pipeline stages (query, formation) log
their timings. Pure-ASGI middleware so SSE streaming stays unbuffered."""

import contextvars
import logging
import time
import uuid

log = logging.getLogger("ybase.http")

request_id_var = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    ))
    handler.addFilter(_RequestIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


class StageTimer:
    """Accumulates named stage durations for one pipeline run."""

    def __init__(self) -> None:
        self._stages: "dict[str, float]" = {}
        self._mark = time.perf_counter()

    def lap(self, stage: str) -> None:
        now = time.perf_counter()
        self._stages[stage] = self._stages.get(stage, 0.0) + (now - self._mark)
        self._mark = now

    def line(self) -> str:
        return " ".join(f"{k}={v * 1000:.0f}ms" for k, v in self._stages.items())


class RequestContextMiddleware:
    """Assigns a request id (echoed as X-Request-ID), logs one line per
    request with status and duration."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = {"code": 0}

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-request-id", rid.encode())
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            dur_ms = (time.perf_counter() - start) * 1000
            path = scope.get("path", "")
            logger = log.debug if path in ("/api/health",) else log.info
            logger("%s %s -> %d in %.1fms",
                   scope.get("method", "?"), path, status["code"], dur_ms)
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Baseline security headers. No CSP yet — the built UI would need its
    inline styles/scripts audited first. HSTS only makes sense behind HTTPS,
    so it keys off SESSION_COOKIE_SECURE."""

    HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    ]
    HSTS = (b"strict-transport-security", b"max-age=63072000; includeSubDomains")

    def __init__(self, app, hsts: bool = False) -> None:
        self.app = app
        self.headers = self.HEADERS + ([self.HSTS] if hsts else [])

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(self.headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)
