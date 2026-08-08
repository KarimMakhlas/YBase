"""Top-level API router wiring.

The individual route modules keep their existing paths/prefixes. This file is
the single place that assembles them onto the FastAPI app.
"""

from fastapi import APIRouter

from ..domains.agent import service as agent
from ..domains.auth import service as auth
from ..domains.billing import service as billing
from ..domains.connectors import service as sources
from ..domains.memory import review_service as memory_review
from .routes import ALL_ROUTERS as DOMAIN_ROUTERS

ALL_ROUTERS = [
    agent.router,
    auth.router,
    billing.router,
    sources.router,
    memory_review.router,
    *DOMAIN_ROUTERS,
]

api_router = APIRouter()
for router in ALL_ROUTERS:
    api_router.include_router(router)
