import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.router import api_router
from .core import config, db, mailer, migrate
from .domains.connectors import service as sources
from .domains.memory import worker
from .core.observability import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    setup_logging,
    setup_sentry,
)

setup_logging()
setup_sentry()

log = logging.getLogger("ybase.startup")


def _warn_on_missing_email_provider() -> None:
    """mailer.send() is a silent no-op without RESEND_API_KEY, and /api/auth/forgot
    returns ok either way (deliberately — it must not leak which addresses exist).
    The upshot is that on a public instance with no provider, password reset and
    email verification appear to work and simply never arrive. Say so loudly at
    boot instead of letting a user discover it."""
    if mailer.configured():
        return
    if config.ALLOW_PUBLIC_SIGNUP:
        log.warning(
            "No email provider configured (RESEND_API_KEY + DIGEST_FROM_EMAIL) while "
            "public signup is enabled — password-reset and email-verification links "
            "will NOT be delivered."
        )
    else:
        log.info(
            "No email provider configured — password-reset and verification emails "
            "are logged no-ops on this instance."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await migrate.run()
    _warn_on_missing_email_provider()
    runs_workers = config.RUNTIME_ROLE in {"all", "worker"}
    if runs_workers:
        await sources.recover_stuck_sync_jobs()
        await worker.recover_stuck_materialization()
        await worker.recover_stuck()
        worker.start_preprocessing()
        worker.start_formation()
        worker.start_periodic()
    yield
    if runs_workers:
        await sources.stop_sync_tasks()
        await worker.stop()
    await db.close_pool()


app = FastAPI(title="YBase", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware, hsts=config.SESSION_COOKIE_SECURE)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.MAX_REQUEST_BYTES)

app.include_router(api_router)


# ---- Built frontend (docker image) ----

if config.STATIC_DIR and Path(config.STATIC_DIR).is_dir():
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
