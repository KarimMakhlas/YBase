"""Auth request/response schemas.

Re-exported from app.domains.auth.service so callers can import the schema
types from this module without pulling in the service logic.
"""

from .service import (  # noqa: F401
    AuthContext,
    BootstrapRequest,
    JoinRequest,
    LoginRequest,
    RegisterRequest,
    SwitchWorkspaceRequest,
    UserCreateRequest,
    UserPatchRequest,
)
