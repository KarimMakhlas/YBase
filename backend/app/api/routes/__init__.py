"""API route modules, one per domain."""

from . import (  # noqa: F401
    analytics,
    digests,
    documents,
    feedback,
    health,
    integrations,
    memory,
    ops,
    query,
    sessions,
    workspace,
)

ALL_ROUTERS = [
    health.router,
    analytics.router,
    feedback.router,
    digests.router,
    documents.router,
    memory.router,
    ops.router,
    workspace.router,
    sessions.router,
    query.router,
    integrations.router,
]
