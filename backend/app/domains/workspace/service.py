import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import config, db, mailer as email
from app.domains.auth import service as auth

router = APIRouter(prefix="/api", tags=["workspace"])


class InviteCreateRequest(BaseModel):
    role: str = "member"
    email: Optional[str] = None


class WorkspaceCreateRequest(BaseModel):
    name: str = ""


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: int


@router.post("/workspace/create")
async def create_workspace(
    req: WorkspaceCreateRequest,
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Create a workspace and make the current user its sole owner, then point
    this session at it. Called by the onboarding wizard (and 'new workspace')."""
    name = req.name.strip() or f"{current.display_name}'s Workspace"
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            slug = await auth._unique_slug(conn, name)
            workspace_id = await conn.fetchval(
                "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
                name, slug,
            )
            await conn.execute(
                "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                "VALUES($1, $2, 'owner')",
                workspace_id, current.user_id,
            )
            # Re-point the active session at the new workspace (same as switch).
            await conn.execute(
                "UPDATE auth_sessions SET workspace_id=$2, last_seen_at=now() WHERE id=$1",
                current.session_id, workspace_id,
            )
            await auth.audit(conn, "create_workspace", workspace_id, current.user_id,
                             "workspace", workspace_id)
            ctx = await auth._context_for(
                conn, current.user_id, workspace_id, current.session_id
            )
    return auth.user_payload(ctx)


@router.get("/workspace/onboarding")
async def workspace_onboarding(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Setup-checklist state for the current workspace: which onboarding steps
    are done, and whether the owner has finished/dismissed the wizard."""
    if current.workspace_id is None:
        raise HTTPException(409, "create a workspace first")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT "
            "  (SELECT onboarded_at FROM workspaces WHERE id=$1) AS onboarded_at, "
            "  EXISTS(SELECT 1 FROM workspace_invites WHERE workspace_id=$1) AS invited, "
            "  EXISTS(SELECT 1 FROM source_connections WHERE workspace_id=$1) AS connected, "
            "  EXISTS(SELECT 1 FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "         WHERE s.workspace_id=$1 AND m.role='assistant') AS asked",
            current.workspace_id,
        )
    return {
        "onboarded_at": row["onboarded_at"].isoformat() if row["onboarded_at"] else None,
        "role": current.role,
        "steps": {
            "workspace": True,
            "invited": row["invited"],
            "connected": row["connected"],
            "asked": row["asked"],
        },
    }


@router.post("/workspace/onboarding/complete")
async def complete_onboarding(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    """Mark the setup wizard finished/skipped — dismisses the checklist."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE workspaces SET onboarded_at=now() WHERE id=$1 AND onboarded_at IS NULL",
            current.workspace_id,
        )
    return {"ok": True}


@router.post("/workspace/transfer-ownership")
async def transfer_ownership(
    req: TransferOwnershipRequest,
    current: auth.AuthContext = Depends(auth.require_role("owner")),
) -> Dict[str, Any]:
    """Hand the single owner role to another member. Demotes the current owner
    to admin and promotes the target to owner, atomically."""
    if req.new_owner_user_id == current.user_id:
        raise HTTPException(400, "you are already the owner")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            target = await conn.fetchrow(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id=$1 AND user_id=$2 FOR UPDATE",
                current.workspace_id, req.new_owner_user_id,
            )
            if target is None:
                raise HTTPException(404, "that user is not a member of this workspace")
            # Demote first, then promote, so at most one owner exists at any point.
            await conn.execute(
                "UPDATE workspace_memberships SET role='admin' "
                "WHERE workspace_id=$1 AND user_id=$2",
                current.workspace_id, current.user_id,
            )
            await conn.execute(
                "UPDATE workspace_memberships SET role='owner' "
                "WHERE workspace_id=$1 AND user_id=$2",
                current.workspace_id, req.new_owner_user_id,
            )
            await auth.audit(conn, "transfer_ownership", current.workspace_id,
                             current.user_id, "user", req.new_owner_user_id)
    return {"ok": True, "new_owner_user_id": req.new_owner_user_id}


@router.get("/workspace/users")
async def list_workspace_users(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.id, u.email, u.display_name, u.disabled, u.created_at, m.role "
            "FROM workspace_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.workspace_id=$1 ORDER BY m.role, u.email",
            current.workspace_id,
        )
    return [dict(r) for r in rows]


@router.post("/workspace/users")
async def create_workspace_user(
    req: auth.UserCreateRequest,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    # Ownership is single and only moves via transfer-ownership — never minted.
    if req.role not in ("admin", "member"):
        raise HTTPException(400, "role must be admin or member")
    email = auth.clean_email(req.email)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT id FROM users WHERE lower(email)=lower($1)", email
            )
            if exists:
                raise HTTPException(409, "user already exists")
            user_id = await conn.fetchval(
                "INSERT INTO users(email, display_name, password_hash) "
                "VALUES($1, $2, $3) RETURNING id",
                email, req.display_name.strip() or email, auth.hash_password(req.password),
            )
            await conn.execute(
                "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
                "VALUES($1, $2, $3)",
                current.workspace_id, user_id, req.role,
            )
            await auth.audit(conn, "create_user", current.workspace_id, current.user_id,
                             "user", user_id, {"role": req.role})
    return {"id": user_id}


def _invite_path(token: str) -> str:
    return f"/#/join/{token}"


@router.get("/workspace/invites")
async def list_workspace_invites(
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> List[Dict[str, Any]]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT i.id, i.role, i.email, i.created_at, i.expires_at, "
            "       i.accepted_at, i.accepted_by, i.revoked_at, "
            "       u.display_name AS created_by_name, au.email AS accepted_by_email "
            "FROM workspace_invites i "
            "LEFT JOIN users u ON u.id = i.created_by "
            "LEFT JOIN users au ON au.id = i.accepted_by "
            "WHERE i.workspace_id=$1 ORDER BY i.created_at DESC LIMIT 50",
            current.workspace_id,
        )
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if d["revoked_at"] is not None:
            d["state"] = "revoked"
        elif d["accepted_at"] is not None:
            d["state"] = "used"
        elif d["expires_at"] <= now:
            d["state"] = "expired"
        else:
            d["state"] = "active"
        out.append(d)
    return out


@router.post("/workspace/invites")
async def create_workspace_invite(
    req: InviteCreateRequest,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    if req.role not in ("admin", "member"):
        raise HTTPException(400, "invite role must be admin or member")
    invite_email = auth.clean_email(req.email) if req.email else None
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = datetime.now(timezone.utc) + timedelta(days=config.INVITE_TTL_DAYS)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            invite_id = await conn.fetchval(
                "INSERT INTO workspace_invites(workspace_id, token_hash, role, email, "
                "created_by, expires_at) VALUES($1, $2, $3, $4, $5, $6) RETURNING id",
                current.workspace_id, token_hash, req.role, invite_email,
                current.user_id, expires,
            )
            await auth.audit(conn, "create_invite", current.workspace_id, current.user_id,
                             "invite", invite_id, {"role": req.role})
    # Email the link when an address was given and a provider is configured.
    email_result = {"status": "skipped", "reason": "no email address"}
    if invite_email:
        url = f"{config.APP_BASE_URL.rstrip('/')}{_invite_path(token)}"
        body = (
            f"You've been invited to join {current.workspace_name} on WhyBase "
            f"as {req.role}.\n\nJoin here:\n{url}\n\n"
            f"This link expires {expires.date().isoformat()}."
        )
        email_result = await email.send(
            [invite_email], f"Join {current.workspace_name} on WhyBase", body
        )
    return {
        "id": invite_id,
        "token": token,
        "path": _invite_path(token),
        "role": req.role,
        "expires_at": expires.isoformat(),
        "email": email_result,
    }


@router.delete("/workspace/invites/{invite_id}")
async def revoke_workspace_invite(
    invite_id: int,
    current: auth.AuthContext = Depends(auth.require_role("admin")),
) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE workspace_invites SET revoked_at=now() "
                "WHERE id=$1 AND workspace_id=$2 AND accepted_at IS NULL AND revoked_at IS NULL "
                "RETURNING id",
                invite_id, current.workspace_id,
            )
            if row is None:
                raise HTTPException(404, "invite not found or no longer active")
            await auth.audit(conn, "revoke_invite", current.workspace_id, current.user_id,
                             "invite", invite_id)
    return {"id": invite_id, "revoked": True}


@router.patch("/workspace/users/{user_id}")
async def patch_workspace_user(
    user_id: int,
    req: auth.UserPatchRequest,
    current: auth.AuthContext = Depends(auth.require_role("owner")),
) -> Dict[str, Any]:
    # Ownership moves only via transfer-ownership; this path can set admin/member.
    if req.role is not None and req.role not in ("admin", "member"):
        raise HTTPException(400, "role must be admin or member (use transfer-ownership)")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            membership = await conn.fetchrow(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id=$1 AND user_id=$2",
                current.workspace_id, user_id,
            )
            if membership is None:
                raise HTTPException(404, "user not found in workspace")
            owner_count = await conn.fetchval(
                "SELECT count(*) FROM workspace_memberships "
                "WHERE workspace_id=$1 AND role='owner'",
                current.workspace_id,
            )
            removing_last_owner = (
                membership["role"] == "owner"
                and owner_count <= 1
                and ((req.role is not None and req.role != "owner") or req.disabled is True)
            )
            if removing_last_owner:
                raise HTTPException(400, "cannot remove the last owner")
            if req.display_name is not None:
                await conn.execute(
                    "UPDATE users SET display_name=$2, updated_at=now() WHERE id=$1",
                    user_id, req.display_name.strip() or "Unnamed user",
                )
            if req.password is not None:
                await conn.execute(
                    "UPDATE users SET password_hash=$2, updated_at=now() WHERE id=$1",
                    user_id, auth.hash_password(req.password),
                )
            if req.disabled is not None:
                await conn.execute(
                    "UPDATE users SET disabled=$2, updated_at=now() WHERE id=$1",
                    user_id, req.disabled,
                )
                if req.disabled:
                    await conn.execute(
                        "UPDATE auth_sessions SET revoked_at=now() "
                        "WHERE user_id=$1 AND revoked_at IS NULL",
                        user_id,
                    )
            if req.role is not None:
                await conn.execute(
                    "UPDATE workspace_memberships SET role=$3 "
                    "WHERE workspace_id=$1 AND user_id=$2",
                    current.workspace_id, user_id, req.role,
                )
            audit_data = req.model_dump(exclude_none=True)
            if "password" in audit_data:
                audit_data["password_changed"] = True
                audit_data.pop("password", None)
            await auth.audit(conn, "patch_user", current.workspace_id, current.user_id,
                             "user", user_id, audit_data)
    return {"id": user_id}
