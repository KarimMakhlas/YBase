from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.router import api_router
from .core import config, db
from .domains.memory import worker
from .core.observability import (
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    setup_logging,
)

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_schema()
    await worker.recover_stuck()
    worker.start()
    yield
    await worker.stop()
    await db.close_pool()


app = FastAPI(title="WhyBase", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(SecurityHeadersMiddleware, hsts=config.SESSION_COOKIE_SECURE)

app.include_router(api_router)


# ---- Built frontend (docker image) ----

if config.STATIC_DIR and Path(config.STATIC_DIR).is_dir():
    app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
