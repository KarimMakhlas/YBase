"""Password auth, workspace membership, and role dependencies."""

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import asyncpg
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core import config, db, mailer
from app.core.ratelimit import auth_limiter

logger = logging.getLogger(__name__)

COOKIE_NAME = "sb_session"
_ph = PasswordHasher()
_ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


@dataclass
class AuthContext:
    user_id: int
    email: str
    display_name: str
    # A freshly-registered user has no active workspace until the setup wizard
    # creates one, so these are None during onboarding.
    workspace_id: Optional[int]
    workspace_name: Optional[str]
    role: Optional[str]
    session_id: int
    workspaces: List[Dict[str, Any]]
    # Billing state of the active workspace (None while workspace-less). Carried
    # here so the write-gate needs no extra query — see require_writable_workspace.
    plan_status: Optional[str] = None
    trial_ends_at: Optional[datetime] = None
    # 'password' or 'google' — lets the account page hide the password form for
    # Google-only sign-ins.
    auth_provider: str = "password"
    # Whether this account has proven it owns its email address. Google accounts
    # and every account predating verification are treated as verified; only new
    # password signups start False. Drives the "verify your email" banner and,
    # more importantly, whether Google sign-in may auto-link into this account.
    email_verified: bool = True


class BootstrapRequest(BaseModel):
    workspace_name: str = "Default Workspace"
    email: str
    display_name: str
    password: str


class RegisterRequest(BaseModel):
    # Identity-only signup. The workspace is named later, in the setup wizard
    # (POST /api/workspace/create). workspace_name is accepted but ignored for
    # backward compatibility with older clients.
    email: str
    display_name: str
    password: str
    workspace_name: str = ""


class JoinRequest(BaseModel):
    token: str
    email: str
    display_name: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str
    workspace_id: Optional[int] = None


class SwitchWorkspaceRequest(BaseModel):
    workspace_id: int


class UserCreateRequest(BaseModel):
    email: str
    display_name: str
    password: str
    role: str = "member"


class UserPatchRequest(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None


class MePatchRequest(BaseModel):
    """Self-service profile edit. Changing the password requires the current one."""
    display_name: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class VerifyEmailRequest(BaseModel):
    token: str


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _reset_path(token: str) -> str:
    return f"/#/reset/{token}"


def _verify_path(token: str) -> str:
    return f"/#/verify/{token}"


async def send_verification_email(
    conn: asyncpg.Connection, user_id: int, email: str, display_name: str
) -> None:
    """Mint a single-use verification token and email the link. Best effort:
    mailer.send never raises, and without RESEND_API_KEY it is a logged no-op —
    so signup still succeeds on an instance with no email provider (which is
    also why verification does not gate sign-in)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=config.VERIFICATION_TTL_HOURS)
    await conn.execute(
        "INSERT INTO email_verification_tokens(user_id, token_hash, expires_at) "
        "VALUES($1, $2, $3)",
        user_id, _hash_token(token), expires,
    )
    link = f"{config.APP_BASE_URL.rstrip('/')}{_verify_path(token)}"
    await mailer.send(
        [email],
        "Verify your YBase email",
        f"Hi {display_name},\n\n"
        f"Confirm this address to finish setting up your YBase account "
        f"(link valid for {config.VERIFICATION_TTL_HOURS} hours):\n\n{link}\n\n"
        "If you didn't create a YBase account, you can safely ignore this email.",
    )


def _client_ip(request: Request) -> str:
    # A platform-injected real-client-IP header (fly-client-ip, cf-connecting-ip,
    # …) is the only value the client cannot forge, so prefer it when configured.
    if config.REAL_IP_HEADER:
        real = request.headers.get(config.REAL_IP_HEADER)
        if real:
            return real.strip()
    # Otherwise trust the RIGHTMOST X-Forwarded-For entry: with a single trusted
    # proxy in front (the typical PaaS setup) that is the IP the proxy actually
    # observed. The leftmost entries are client-supplied and spoofable — taking
    # the first one (the old behaviour) let an attacker forge their rate-limit key.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else ""


def _slug(value: str) -> str:
    chars = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            chars.append(ch)
            last_dash = False
        elif not last_dash:
            chars.append("-")
            last_dash = True
    slug = "".join(chars).strip("-")
    return slug or "workspace"


async def _unique_slug(conn: asyncpg.Connection, name: str) -> str:
    """A slug not already taken — public signup allows duplicate workspace names."""
    base = _slug(name)
    slug = base
    n = 2
    while await conn.fetchval(
        "SELECT 1 FROM workspaces WHERE lower(slug)=lower($1)", slug
    ):
        slug = f"{base}-{n}"
        n += 1
    return slug


def clean_email(email: str) -> str:
    email = " ".join((email or "").split()).lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(400, "valid email is required")
    return email


def _verify_sync(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


async def hash_password(password: str) -> str:
    """Argon2 hash, computed off the event loop.

    Argon2 is deliberately expensive (~30ms here, 100-200ms on a shared vCPU)
    and the hasher is synchronous C code. Called inline it blocks the whole
    single-worker event loop for that long, so one login stalls every in-flight
    SSE query stream — and a modest burst of login attempts becomes a cheap DoS.
    The length check stays on the loop so the 400 raises without a thread hop."""
    if len(password) < config.PASSWORD_MIN_LENGTH:
        raise HTTPException(
            400,
            f"password must be at least {config.PASSWORD_MIN_LENGTH} characters",
        )
    return await asyncio.to_thread(_ph.hash, password)


async def verify_password(password: str, password_hash: Optional[str]) -> bool:
    """Argon2 verify, off the event loop for the same reason as hash_password.
    A missing hash (Google-only account) is a plain False, not a thread hop."""
    if not password_hash:
        return False
    return await asyncio.to_thread(_verify_sync, password, password_hash)


async def audit(
    conn: asyncpg.Connection,
    action: str,
    workspace_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_events(workspace_id, actor_user_id, action, target_type, target_id, data) "
        "VALUES($1, $2, $3, $4, $5, $6)",
        workspace_id, actor_user_id, action, target_type,
        str(target_id) if target_id is not None else None, data or {},
    )


async def bootstrap_needed() -> bool:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM users")
    return count == 0


async def _memberships(conn: asyncpg.Connection, user_id: int) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT w.id, w.name, m.role "
        "FROM workspace_memberships m JOIN workspaces w ON w.id = m.workspace_id "
        "WHERE m.user_id=$1 ORDER BY w.name",
        user_id,
    )
    return [dict(r) for r in rows]


async def _context_for(
    conn: asyncpg.Connection,
    user_id: int,
    workspace_id: Optional[int],
    session_id: int,
) -> AuthContext:
    if workspace_id is None:
        # Onboarding state: the user exists but hasn't created a workspace yet.
        urow = await conn.fetchrow(
            "SELECT id AS user_id, email, display_name, auth_provider, email_verified_at "
            "FROM users WHERE id=$1",
            user_id,
        )
        if urow is None:
            raise HTTPException(404, "user not found")
        return AuthContext(
            user_id=urow["user_id"],
            email=urow["email"],
            display_name=urow["display_name"],
            workspace_id=None,
            workspace_name=None,
            role=None,
            session_id=session_id,
            workspaces=await _memberships(conn, user_id),
            auth_provider=urow["auth_provider"],
            email_verified=urow["email_verified_at"] is not None,
        )
    row = await conn.fetchrow(
        "SELECT u.id AS user_id, u.email, u.display_name, u.auth_provider, u.email_verified_at, "
        "       w.name AS workspace_name, m.role, w.plan_status, w.trial_ends_at "
        "FROM users u "
        "JOIN workspace_memberships m ON m.user_id = u.id "
        "JOIN workspaces w ON w.id = m.workspace_id "
        "WHERE u.id=$1 AND m.workspace_id=$2",
        user_id, workspace_id,
    )
    if row is None:
        raise HTTPException(403, "user has no workspace membership")
    return AuthContext(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        workspace_id=workspace_id,
        workspace_name=row["workspace_name"],
        role=row["role"],
        session_id=session_id,
        workspaces=await _memberships(conn, user_id),
        plan_status=row["plan_status"],
        trial_ends_at=row["trial_ends_at"],
        auth_provider=row["auth_provider"],
        email_verified=row["email_verified_at"] is not None,
    )


async def _record_activity(
    conn: asyncpg.Connection, workspace_id: int, user_id: int
) -> None:
    """Mark the user active today (UTC). Idempotent per user/workspace/day."""
    await conn.execute(
        "INSERT INTO activity_days(workspace_id, user_id, day) "
        "VALUES($1, $2, (now() AT TIME ZONE 'utc')::date) ON CONFLICT DO NOTHING",
        workspace_id, user_id,
    )


async def create_session(
    conn: asyncpg.Connection,
    user_id: int,
    workspace_id: Optional[int],
    request: Request,
    response: Response,
) -> int:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=config.SESSION_DAYS)
    session_id = await conn.fetchval(
        "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, user_agent, ip, expires_at) "
        "VALUES($1, $2, $3, $4, $5, $6) RETURNING id",
        user_id, workspace_id, _hash_token(token),
        request.headers.get("user-agent"), _client_ip(request), expires,
    )
    if workspace_id is not None:
        await _record_activity(conn, workspace_id, user_id)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=config.SESSION_COOKIE_SECURE,
        path="/",
    )
    return session_id


async def revoke_current_session(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE_NAME)
    response.delete_cookie(COOKIE_NAME, path="/")
    if not token:
        return
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE auth_sessions SET revoked_at=now() "
            "WHERE token_hash=$1 AND revoked_at IS NULL",
            _hash_token(token),
        )


async def get_current_user(request: Request) -> AuthContext:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "not authenticated")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        # LEFT JOIN workspace/membership: an onboarding session has no workspace
        # yet (s.workspace_id IS NULL), and must still authenticate.
        row = await conn.fetchrow(
            "SELECT s.id AS session_id, s.workspace_id, s.last_seen_at, u.id AS user_id, "
            "       u.email, u.display_name, u.disabled, u.auth_provider, u.email_verified_at, "
            "       w.name AS workspace_name, m.role, w.plan_status, w.trial_ends_at "
            "FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id "
            "LEFT JOIN workspaces w ON w.id = s.workspace_id "
            "LEFT JOIN workspace_memberships m ON m.user_id = u.id AND m.workspace_id = s.workspace_id "
            "WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at > now()",
            _hash_token(token),
        )
        if row is None or row["disabled"]:
            raise HTTPException(401, "not authenticated")
        # Record a daily-active row only on the first request of a new UTC day
        # (keeps analytics accurate without a write on every request).
        prev_seen = row["last_seen_at"]
        if row["workspace_id"] is not None and (
            prev_seen is None
            or prev_seen.astimezone(timezone.utc).date() < datetime.now(timezone.utc).date()
        ):
            await _record_activity(conn, row["workspace_id"], row["user_id"])
        await conn.execute(
            "UPDATE auth_sessions SET last_seen_at=now() WHERE id=$1", row["session_id"]
        )
        workspaces = await _memberships(conn, row["user_id"])
    return AuthContext(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        workspace_id=row["workspace_id"],
        workspace_name=row["workspace_name"],
        role=row["role"],
        session_id=row["session_id"],
        workspaces=workspaces,
        plan_status=row["plan_status"],
        trial_ends_at=row["trial_ends_at"],
        auth_provider=row["auth_provider"],
        email_verified=row["email_verified_at"] is not None,
    )


def workspace_writable(plan_status: Optional[str], trial_ends_at: Optional[datetime]) -> bool:
    """Whether a workspace currently accepts writes. Trial expiry is lazy: a
    'trialing' workspace whose trial_ends_at has passed is treated as expired.
    trial_ends_at is None for self-hosted/legacy workspaces, which never expire."""
    if plan_status == "active":
        return True
    if plan_status == "trialing":
        return trial_ends_at is None or trial_ends_at > datetime.now(timezone.utc)
    return False  # past_due, expired, or anything unrecognized → read-only


def require_role(min_role: str):
    async def dep(user: AuthContext = Depends(get_current_user)) -> AuthContext:
        # role is None during onboarding (no workspace yet) → ranks 0 → blocked.
        if _ROLE_RANK.get(user.role or "", 0) < _ROLE_RANK[min_role]:
            raise HTTPException(403, "insufficient role")
        return user
    return dep


async def assert_workspace_writable(workspace_id: int) -> None:
    """402 when the workspace is read-only (trial expired or payment past due).

    The session path gets plan state for free on AuthContext; an AgentContext
    doesn't carry it (an API key acts for a workspace, not a person), so the
    machine-facing write routes read it here rather than skipping the gate."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan_status, trial_ends_at FROM workspaces WHERE id=$1", workspace_id
        )
    if row is None or not workspace_writable(row["plan_status"], row["trial_ends_at"]):
        raise HTTPException(402, "workspace is read-only — upgrade to keep editing")


def require_writable_workspace(min_role: str = "member"):
    """Like require_role, but also 402s when the workspace is read-only (trial
    expired or payment past due). Use on every mutating route except auth and
    billing — reads stay open so an expired workspace can still browse its data."""
    async def dep(user: AuthContext = Depends(get_current_user)) -> AuthContext:
        if _ROLE_RANK.get(user.role or "", 0) < _ROLE_RANK[min_role]:
            raise HTTPException(403, "insufficient role")
        if not workspace_writable(user.plan_status, user.trial_ends_at):
            raise HTTPException(
                402, "workspace is read-only — upgrade to keep editing"
            )
        return user
    return dep


# ── API keys (machine auth for /api/agent/*) ─────────────────────────────────

API_KEY_PREFIX = "ybk_"


@dataclass
class AgentContext:
    """Auth context for API-key (machine) callers. Deliberately a sibling of
    AuthContext without user/session fields: an agent acts for a workspace,
    not as a person, and nothing downstream should pretend otherwise.

    allowed_topics None = unrestricted; a list means the key only sees and
    proposes memory linked to those topics (matched on lowercased label)."""
    workspace_id: int
    workspace_name: str
    key_id: int
    key_name: str
    allowed_topics: Optional[List[str]] = None


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_hex(20)


async def require_api_key(request: Request) -> AgentContext:
    """Authenticate `Authorization: Bearer ybk_...`. Reads stay open regardless
    of plan status, matching the stance on session reads. Write routes on the
    agent API (currently /api/agent/propose) call assert_workspace_writable
    themselves — this dependency only establishes identity."""
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not token.startswith(API_KEY_PREFIX):
        raise HTTPException(401, "missing or malformed API key")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT k.id, k.name, k.last_used_at, k.workspace_id, k.allowed_topics, "
            "w.name AS workspace_name "
            "FROM api_keys k JOIN workspaces w ON w.id = k.workspace_id "
            "WHERE k.token_hash=$1 AND k.revoked_at IS NULL",
            _hash_token(token),
        )
        if row is None:
            raise HTTPException(401, "invalid or revoked API key")
        # Throttled liveness marker: one write per key per minute, not per call.
        last = row["last_used_at"]
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > 60:
            await conn.execute(
                "UPDATE api_keys SET last_used_at=now() WHERE id=$1", row["id"]
            )
    return AgentContext(
        workspace_id=row["workspace_id"],
        workspace_name=row["workspace_name"],
        key_id=row["id"],
        key_name=row["name"],
        allowed_topics=list(row["allowed_topics"]) if row["allowed_topics"] is not None else None,
    )



async def _login_throttled(conn: asyncpg.Connection, email: str, ip: str) -> bool:
    failures = await conn.fetchval(
        "SELECT count(*) FROM auth_login_attempts "
        "WHERE lower(email)=lower($1) AND COALESCE(ip, '')=COALESCE($2, '') "
        "AND success=false "
        "AND attempted_at > now() - ($3 || ' minutes')::interval",
        email, ip, str(config.LOGIN_WINDOW_MINUTES),
    )
    return failures >= config.LOGIN_MAX_FAILURES


async def _record_login_attempt(
    conn: asyncpg.Connection, email: str, ip: str, success: bool
) -> None:
    await conn.execute(
        "INSERT INTO auth_login_attempts(email, ip, success) VALUES($1, $2, $3)",
        email, ip, success,
    )


def user_payload(user: AuthContext) -> Dict[str, Any]:
    return {
        "user": {
            "id": user.user_id,
            "email": user.email,
            "display_name": user.display_name,
            "auth_provider": user.auth_provider,
            "email_verified": user.email_verified,
        },
        # null while the user is mid-onboarding (no workspace created yet) —
        # the frontend renders the setup wizard in that case.
        "workspace": (
            {
                "id": user.workspace_id,
                "name": user.workspace_name,
                "role": user.role,
            }
            if user.workspace_id is not None
            else None
        ),
        "workspaces": user.workspaces,
    }


@router.get("/bootstrap-status")
async def bootstrap_status() -> Dict[str, Any]:
    return {"needs_bootstrap": await bootstrap_needed()}


@router.post("/bootstrap")
async def bootstrap(
    req: BootstrapRequest, request: Request, response: Response
) -> Dict[str, Any]:
    pool = await db.get_pool()
    email = clean_email(req.email)
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval("SELECT count(*) FROM users")
            if existing:
                raise HTTPException(409, "bootstrap already completed")
            workspace = await conn.fetchrow(
                "SELECT id FROM workspaces WHERE lower(slug)='default' LIMIT 1"
            )
            if workspace is None:
                workspace_id = await conn.fetchval(
                    "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                    req.workspace_name.strip() or "Default Workspace",
                    _slug(req.workspace_name),
                )
            else:
                workspace_id = workspace["id"]
                await conn.execute(
                    "UPDATE workspaces SET name=$2, slug=$3 WHERE id=$1",
                    workspace_id,
                    req.workspace_name.strip() or "Default Workspace",
                    _slug(req.workspace_name),
                )
            # The bootstrap owner is verified by construction: this route only
            # runs on an empty users table, on an install that may well have no
            # email provider at all. Nothing to squat, nobody to notify.
            user_id = await conn.fetchval(
                "INSERT INTO users(email, display_name, password_hash, email_verified_at) "
                "VALUES($1, $2, $3, now()) RETURNING id",
                email, req.display_name.strip() or email,
                await hash_password(req.password),
            )
            await conn.execute(
                "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                "VALUES($1, $2, 'owner')",
                workspace_id, user_id,
            )
            await conn.execute(
                "UPDATE chat_sessions SET user_id=$2 WHERE workspace_id=$1 AND user_id IS NULL",
                workspace_id, user_id,
            )
            session_id = await create_session(conn, user_id, workspace_id, request, response)
            await audit(conn, "bootstrap", workspace_id, user_id, "workspace", workspace_id)
            user = await _context_for(conn, user_id, workspace_id, session_id)
            return user_payload(user)


@router.post("/register")
async def register(
    req: RegisterRequest, request: Request, response: Response
) -> Dict[str, Any]:
    """Public self-serve signup — identity only. Creates the user account and a
    workspace-less session; the setup wizard then calls POST /api/workspace/create
    to name the workspace and make the user its owner."""
    if not config.ALLOW_PUBLIC_SIGNUP:
        raise HTTPException(403, "public signup is disabled on this instance")
    await auth_limiter.enforce(_client_ip(request), "signup")
    email = clean_email(req.email)
    display_name = req.display_name.strip() or email
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE lower(email)=lower($1)", email
            )
            if existing:
                raise HTTPException(
                    409, "an account with this email already exists — sign in instead"
                )
            password_hash = await hash_password(req.password)  # validates length
            user_id = await conn.fetchval(
                "INSERT INTO users(email, display_name, password_hash) VALUES($1, $2, $3) "
                "RETURNING id",
                email, display_name, password_hash,
            )
            session_id = await create_session(conn, user_id, None, request, response)
            await audit(conn, "register", None, user_id, "user", user_id)
            # New password signups start unverified — this is what stops a
            # squatted address from later absorbing the real owner's Google
            # identity. See _google_find_or_create.
            await send_verification_email(conn, user_id, email, display_name)
            user = await _context_for(conn, user_id, None, session_id)
    return user_payload(user)


@router.get("/invite/{token}")
async def invite_preview(token: str) -> Dict[str, Any]:
    """Public: show which workspace an invite link joins, before sign-up."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT i.role, i.email, i.expires_at, i.accepted_at, i.revoked_at, "
            "       w.name AS workspace_name "
            "FROM workspace_invites i JOIN workspaces w ON w.id = i.workspace_id "
            "WHERE i.token_hash=$1",
            _hash_token(token),
        )
    if row is None:
        raise HTTPException(404, "invite not found")
    if row["revoked_at"] is not None:
        reason = "revoked"
    elif row["accepted_at"] is not None:
        reason = "used"
    elif row["expires_at"] <= datetime.now(timezone.utc):
        reason = "expired"
    else:
        reason = None
    return {
        "valid": reason is None,
        "reason": reason,
        "workspace_name": row["workspace_name"],
        "role": row["role"],
        "email": row["email"],
    }


@router.post("/join")
async def join(
    req: JoinRequest, request: Request, response: Response
) -> Dict[str, Any]:
    """Accept a workspace invite. Creates the account on first join, or attaches
    an existing account (after verifying its password) to the new workspace."""
    email = clean_email(req.email)
    pool = await db.get_pool()
    ip = _client_ip(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            invite = await conn.fetchrow(
                "SELECT id, workspace_id, role, expires_at, accepted_at, revoked_at "
                "FROM workspace_invites WHERE token_hash=$1 FOR UPDATE",
                _hash_token(req.token),
            )
            if invite is None:
                raise HTTPException(404, "invite not found")
            if invite["revoked_at"] is not None:
                raise HTTPException(410, "this invite has been revoked")
            if invite["accepted_at"] is not None:
                raise HTTPException(410, "this invite has already been used")
            if invite["expires_at"] <= datetime.now(timezone.utc):
                raise HTTPException(410, "this invite has expired")
            workspace_id = invite["workspace_id"]
            user = await conn.fetchrow(
                "SELECT id, password_hash, disabled FROM users WHERE lower(email)=lower($1)",
                email,
            )
            if user is None:
                user_id = await conn.fetchval(
                    "INSERT INTO users(email, display_name, password_hash) "
                    "VALUES($1, $2, $3) RETURNING id",
                    email, req.display_name.strip() or email,
                    await hash_password(req.password),
                )
                # An invite link can be pasted anywhere and the invite's email
                # is optional, so joining proves workspace access, not address
                # ownership. New accounts still have to verify.
                await send_verification_email(
                    conn, user_id, email, req.display_name.strip() or email
                )
            else:
                # Existing account: require its password so an invite link can't
                # be used to hijack someone else's email.
                ok = not user["disabled"] and await verify_password(
                    req.password, user["password_hash"]
                )
                await _record_login_attempt(conn, email, ip, ok)
                if not ok:
                    raise HTTPException(
                        401,
                        "an account with this email exists — enter its password to join",
                    )
                user_id = user["id"]
            already_member = await conn.fetchval(
                "SELECT 1 FROM workspace_memberships WHERE workspace_id=$1 AND user_id=$2",
                workspace_id, user_id,
            )
            if already_member is None:
                await conn.execute(
                    "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                    "VALUES($1, $2, $3)",
                    workspace_id, user_id, invite["role"],
                )
            await conn.execute(
                "UPDATE workspace_invites SET accepted_at=now(), accepted_by=$2 WHERE id=$1",
                invite["id"], user_id,
            )
            session_id = await create_session(conn, user_id, workspace_id, request, response)
            await audit(conn, "join_workspace", workspace_id, user_id, "workspace",
                        workspace_id, {"role": invite["role"]})
            current = await _context_for(conn, user_id, workspace_id, session_id)
            return user_payload(current)


@router.post("/login")
async def login(
    req: LoginRequest, request: Request, response: Response
) -> Dict[str, Any]:
    pool = await db.get_pool()
    email = clean_email(req.email)
    ip = _client_ip(request)
    await auth_limiter.enforce(ip, "login")
    async with pool.acquire() as conn:
        if await _login_throttled(conn, email, ip):
            raise HTTPException(429, "too many failed login attempts")
        user = await conn.fetchrow(
            "SELECT id, email, display_name, password_hash, disabled "
            "FROM users WHERE lower(email)=lower($1)",
            email,
        )
        # A Google-only account has no password to check — point them at the
        # right door instead of a generic "invalid password".
        if user and not user["disabled"] and user["password_hash"] is None:
            await _record_login_attempt(conn, email, ip, False)
            raise HTTPException(
                401, "this account uses Google sign-in — use “Continue with Google”"
            )
        ok = bool(user) and not user["disabled"] and await verify_password(
            req.password, user["password_hash"]
        )
        await _record_login_attempt(conn, email, ip, ok)
        if not ok:
            raise HTTPException(401, "invalid email or password")
        memberships = await _memberships(conn, user["id"])
        if not memberships:
            raise HTTPException(403, "user has no workspace membership")
        workspace_id = req.workspace_id or memberships[0]["id"]
        if workspace_id not in {m["id"] for m in memberships}:
            raise HTTPException(403, "not a member of that workspace")
        async with conn.transaction():
            session_id = await create_session(conn, user["id"], workspace_id, request, response)
            await audit(conn, "login", workspace_id, user["id"], "user", user["id"])
            current = await _context_for(conn, user["id"], workspace_id, session_id)
            return user_payload(current)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: AuthContext = Depends(get_current_user),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await audit(conn, "logout", user.workspace_id, user.user_id, "user", user.user_id)
    await revoke_current_session(request, response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    user: AuthContext = Depends(get_current_user),
) -> Dict[str, Any]:
    """Sign out on every device: revoke all of this user's sessions, this one
    included, and clear the cookie."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE auth_sessions SET revoked_at=now() "
            "WHERE user_id=$1 AND revoked_at IS NULL",
            user.user_id,
        )
        await audit(conn, "logout_all", user.workspace_id, user.user_id, "user", user.user_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: AuthContext = Depends(get_current_user)) -> Dict[str, Any]:
    return user_payload(user)


@router.patch("/me")
async def patch_me(
    req: MePatchRequest,
    user: AuthContext = Depends(get_current_user),
) -> Dict[str, Any]:
    """Self-service: edit your own display name and/or password. A password
    change requires the current password and signs out your other sessions."""
    changed: List[str] = []
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if req.new_password is not None:
                row = await conn.fetchrow(
                    "SELECT password_hash FROM users WHERE id=$1", user.user_id
                )
                if row is None or not await verify_password(
                    req.current_password or "", row["password_hash"]
                ):
                    raise HTTPException(403, "current password is incorrect")
                new_hash = await hash_password(req.new_password)  # validates length
                await conn.execute(
                    "UPDATE users SET password_hash=$2, updated_at=now() WHERE id=$1",
                    user.user_id, new_hash,
                )
                # A password change signs out other devices; keep this session.
                await conn.execute(
                    "UPDATE auth_sessions SET revoked_at=now() "
                    "WHERE user_id=$1 AND id<>$2 AND revoked_at IS NULL",
                    user.user_id, user.session_id,
                )
                await audit(conn, "password_change", user.workspace_id,
                            user.user_id, "user", user.user_id)
                changed.append("password")
            if req.display_name is not None:
                name = req.display_name.strip()
                if not name:
                    raise HTTPException(400, "display name cannot be empty")
                await conn.execute(
                    "UPDATE users SET display_name=$2, updated_at=now() WHERE id=$1",
                    user.user_id, name,
                )
                await audit(conn, "profile_update", user.workspace_id,
                            user.user_id, "user", user.user_id)
                changed.append("display_name")
            current = await _context_for(
                conn, user.user_id, user.workspace_id, user.session_id
            )
    payload = user_payload(current)
    payload["changed"] = changed
    return payload


@router.post("/forgot")
async def forgot_password(
    req: ForgotPasswordRequest, request: Request
) -> Dict[str, Any]:
    """Start a password reset. Always returns ok (no account enumeration); when
    the email maps to an active account, emails a time-limited reset link."""
    await auth_limiter.enforce(_client_ip(request), "password reset")
    email = clean_email(req.email)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, display_name, disabled FROM users WHERE lower(email)=lower($1)",
            email,
        )
        if user and not user["disabled"]:
            token = secrets.token_urlsafe(32)
            expires = datetime.now(timezone.utc) + timedelta(
                minutes=config.PASSWORD_RESET_TTL_MINUTES
            )
            await conn.execute(
                "INSERT INTO password_reset_tokens(user_id, token_hash, expires_at) "
                "VALUES($1, $2, $3)",
                user["id"], _hash_token(token), expires,
            )
            link = f"{config.APP_BASE_URL.rstrip('/')}{_reset_path(token)}"
            await mailer.send(
                [email],
                "Reset your YBase password",
                f"Hi {user['display_name']},\n\n"
                f"Use this link to reset your YBase password (valid for "
                f"{config.PASSWORD_RESET_TTL_MINUTES} minutes):\n\n{link}\n\n"
                "If you didn't request this, you can safely ignore this email.",
            )
            await audit(conn, "password_reset_request", None, user["id"],
                        "user", user["id"])
    return {"ok": True}


@router.post("/reset")
async def reset_password(
    req: ResetPasswordRequest, request: Request
) -> Dict[str, Any]:
    """Complete a password reset: consume a valid token atomically, set the new
    password, and revoke every session for that user."""
    await auth_limiter.enforce(_client_ip(request), "password reset")
    if len(req.new_password) < config.PASSWORD_MIN_LENGTH:
        raise HTTPException(
            400, f"password must be at least {config.PASSWORD_MIN_LENGTH} characters"
        )
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE password_reset_tokens SET consumed_at=now() "
                "WHERE token_hash=$1 AND consumed_at IS NULL AND expires_at > now() "
                "RETURNING user_id",
                _hash_token(req.token),
            )
            if row is None:
                raise HTTPException(400, "this reset link is invalid or has expired")
            await conn.execute(
                "UPDATE users SET password_hash=$2, updated_at=now() WHERE id=$1",
                row["user_id"], await hash_password(req.new_password),
            )
            await conn.execute(
                "UPDATE auth_sessions SET revoked_at=now() "
                "WHERE user_id=$1 AND revoked_at IS NULL",
                row["user_id"],
            )
            await audit(conn, "password_reset", None, row["user_id"],
                        "user", row["user_id"])
    return {"ok": True}


@router.post("/verify")
async def verify_email(req: VerifyEmailRequest, request: Request) -> Dict[str, Any]:
    """Complete email verification: consume a valid token atomically and stamp
    email_verified_at. Idempotent from the user's point of view — a second click
    on the same link reports already-verified rather than an error, because mail
    clients and link scanners routinely fetch a link more than once."""
    await auth_limiter.enforce(_client_ip(request), "email verification")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE email_verification_tokens SET consumed_at=now() "
                "WHERE token_hash=$1 AND consumed_at IS NULL AND expires_at > now() "
                "RETURNING user_id",
                _hash_token(req.token),
            )
            if row is None:
                # Distinguish "already done" from "bad link": a consumed token
                # whose user is verified is a success, not a dead end.
                spent = await conn.fetchrow(
                    "SELECT u.email_verified_at FROM email_verification_tokens t "
                    "JOIN users u ON u.id = t.user_id WHERE t.token_hash=$1",
                    _hash_token(req.token),
                )
                if spent is not None and spent["email_verified_at"] is not None:
                    return {"ok": True, "already_verified": True}
                raise HTTPException(
                    400, "this verification link is invalid or has expired"
                )
            await conn.execute(
                "UPDATE users SET email_verified_at=now(), updated_at=now() "
                "WHERE id=$1 AND email_verified_at IS NULL",
                row["user_id"],
            )
            await audit(conn, "email_verified", None, row["user_id"],
                        "user", row["user_id"])
    return {"ok": True, "already_verified": False}


@router.post("/resend-verification")
async def resend_verification(
    request: Request,
    user: AuthContext = Depends(get_current_user),
) -> Dict[str, Any]:
    """Re-send the verification email to the signed-in user's own address."""
    await auth_limiter.enforce(_client_ip(request), "email verification")
    if user.email_verified:
        return {"ok": True, "already_verified": True}
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Retire outstanding links so only the newest one works.
            await conn.execute(
                "UPDATE email_verification_tokens SET consumed_at=now() "
                "WHERE user_id=$1 AND consumed_at IS NULL",
                user.user_id,
            )
            await send_verification_email(
                conn, user.user_id, user.email, user.display_name
            )
    return {"ok": True, "already_verified": False, "sent": mailer.configured()}


@router.post("/switch-workspace")
async def switch_workspace(
    req: SwitchWorkspaceRequest,
    user: AuthContext = Depends(get_current_user),
) -> Dict[str, Any]:
    if req.workspace_id not in {w["id"] for w in user.workspaces}:
        raise HTTPException(403, "not a member of that workspace")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE auth_sessions SET workspace_id=$2, last_seen_at=now() WHERE id=$1",
            user.session_id, req.workspace_id,
        )
        await audit(conn, "switch_workspace", req.workspace_id, user.user_id,
                    "workspace", req.workspace_id)
        current = await _context_for(conn, user.user_id, req.workspace_id, user.session_id)
    return user_payload(current)


# ---- Google sign-in (OAuth 2.0 / OpenID Connect) ----

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)


def _google_redirect_uri() -> str:
    return config.GOOGLE_REDIRECT_BASE_URL.rstrip("/") + "/api/auth/google/callback"


def _login_error_redirect(reason: str = "google") -> RedirectResponse:
    base = config.APP_BASE_URL.rstrip("/")
    return RedirectResponse(f"{base}/?auth_error={reason}#/login")


async def _google_fetch_identity(code: str) -> Dict[str, Any]:
    """Exchange an authorization code for tokens, then read the OIDC userinfo.
    Both hops are server-to-server over TLS straight to Google, so the returned
    `sub`/`email` are trustworthy without separately verifying the id_token JWT.
    Factored out so tests can stub the network round-trip."""
    async with httpx.AsyncClient(timeout=20) as cx:
        tok = await cx.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": _google_redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
        tok.raise_for_status()
        access_token = tok.json().get("access_token")
        if not access_token:
            raise ValueError("Google did not return an access token")
        info = await cx.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info.raise_for_status()
        return info.json()


class UnverifiedAccountConflict(Exception):
    """A Google identity matched an existing password account that has never
    proven it owns the address, so auto-linking is refused."""


async def _google_find_or_create(
    conn: asyncpg.Connection, sub: str, email: str, name: str
) -> int:
    """Resolve a Google identity to a user id: match on google_sub, else
    auto-link an existing account by email, else create a fresh passwordless
    account.

    Google asserting the email is only half of what auto-linking needs — the
    LOCAL side has to be trustworthy too. Public signup accepts any address
    without proof, so an attacker could register victim@corp.com, wait for the
    real victim to "Continue with Google", and have their squatted row silently
    adopt that identity — keeping their own password on the victim's account.
    Linking into a password account therefore requires that account to be
    verified; a passwordless row has no such credential to inherit and is safe.
    """
    row = await conn.fetchrow(
        "SELECT id, disabled FROM users WHERE google_sub=$1", sub
    )
    if row is not None:
        if row["disabled"]:
            raise HTTPException(403, "this account is disabled")
        return row["id"]
    row = await conn.fetchrow(
        "SELECT id, disabled, password_hash, email_verified_at FROM users "
        "WHERE lower(email)=lower($1)",
        email,
    )
    if row is not None:
        if row["disabled"]:
            raise HTTPException(403, "this account is disabled")
        if row["password_hash"] is not None and row["email_verified_at"] is None:
            raise UnverifiedAccountConflict(email)
        await conn.execute(
            "UPDATE users SET google_sub=$2, updated_at=now(), "
            "email_verified_at=COALESCE(email_verified_at, now()) WHERE id=$1",
            row["id"], sub,
        )
        return row["id"]
    # Fresh Google account: Google already asserted email_verified upstream.
    return await conn.fetchval(
        "INSERT INTO users(email, display_name, password_hash, auth_provider, google_sub, "
        "email_verified_at) VALUES($1, $2, NULL, 'google', $3, now()) RETURNING id",
        email, name or email, sub,
    )


@router.get("/providers")
async def auth_providers() -> Dict[str, Any]:
    """Which third-party sign-in options this instance has configured."""
    return {"google": google_configured()}


@router.get("/google/start")
async def google_start(request: Request):
    """Begin Google sign-in: stash a single-use state and bounce to Google."""
    if not google_configured():
        raise HTTPException(404, "Google sign-in is not configured")
    await auth_limiter.enforce(_client_ip(request), "login")
    state = secrets.token_urlsafe(32)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_login_states(state, provider, redirect_path, expires_at) "
            "VALUES($1, 'google', $2, now() + interval '10 minutes')",
            state, config.APP_BASE_URL.rstrip("/"),
        )
    params = urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}")


@router.get("/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    """Google redirects here with ?code&state. Consume the state, resolve the
    identity, issue a session cookie, and bounce back into the app — where a
    user with no workspace lands in the Chunk-2 setup wizard."""
    if not code or not state:
        return _login_error_redirect()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        st = await conn.fetchrow(
            "UPDATE oauth_login_states SET consumed_at=now() "
            "WHERE state=$1 AND provider='google' AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING redirect_path",
            state,
        )
    if st is None:
        return _login_error_redirect()
    try:
        identity = await _google_fetch_identity(code)
    except Exception:
        logger.exception("google sign-in: token/userinfo exchange failed")
        return _login_error_redirect()

    sub = identity.get("sub")
    raw_email = identity.get("email")
    # email_verified can come back as a bool or the string "true" from Google.
    verified = identity.get("email_verified")
    verified_ok = verified in (True, "true")
    if not sub or not raw_email or not verified_ok:
        return _login_error_redirect()
    email = clean_email(raw_email)
    name = identity.get("name") or email.split("@")[0]
    redirect_to = (st["redirect_path"] or config.APP_BASE_URL.rstrip("/"))

    redirect = RedirectResponse(redirect_to)
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                user_id = await _google_find_or_create(conn, sub, email, name)
            except UnverifiedAccountConflict:
                # Someone registered this address with a password and never
                # verified it. Refuse the link and send them to sign in with
                # that password instead — the address's real owner can always
                # reset it, while a squatter gains nothing.
                logger.warning(
                    "google sign-in refused: unverified password account exists for %s",
                    email,
                )
                return _login_error_redirect("unverified_account")
            memberships = await _memberships(conn, user_id)
            workspace_id = memberships[0]["id"] if memberships else None
            # Set the session cookie on the redirect response itself — a returned
            # Response bypasses FastAPI's injected-response cookie merging.
            await create_session(conn, user_id, workspace_id, request, redirect)
            await audit(conn, "google_login", workspace_id, user_id, "user", user_id)
    return redirect
