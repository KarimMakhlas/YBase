"""API route modules, one per domain."""

from . import (  # noqa: F401
    documents,
    health,
    integrations,
    memory,
    query,
    sessions,
    workspace,
)

ALL_ROUTERS = [
    health.router,
    documents.router,
    memory.router,
    workspace.router,
    sessions.router,
    query.router,
    integrations.router,
]
