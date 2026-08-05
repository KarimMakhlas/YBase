"""Billing: trial status + a stubbed checkout.

Per-workspace billing — a 7-day no-card trial, then a single paid "Team" plan.
This chunk ships the data model, the read-only gate (auth.require_writable_workspace),
and a *stubbed* checkout that simply flips the workspace to active. Real Stripe
wiring lands in a later chunk; the route contracts here stay stable so that's a
body-only swap.
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core import config, db
from app.domains.auth import service as auth

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _effective_status(plan_status: Optional[str], trial_ends_at: Optional[datetime]) -> str:
    """Report a lazily-expired trial as 'expired' even though the row still says
    'trialing' (no cron flips it)."""
    if (
        plan_status == "trialing"
        and trial_ends_at is not None
        and trial_ends_at <= datetime.now(timezone.utc)
    ):
        return "expired"
    return plan_status or "unknown"


def _days_left(plan_status: Optional[str], trial_ends_at: Optional[datetime]) -> Optional[int]:
    if plan_status != "trialing" or trial_ends_at is None:
        return None
    remaining = trial_ends_at - datetime.now(timezone.utc)
    return max(0, math.ceil(remaining.total_seconds() / 86400))


@router.get("/status")
async def billing_status(
    current: auth.AuthContext = Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Trial/plan state for the active workspace. Open even when read-only so the
    upgrade UI can render. Computed from AuthContext — no extra query."""
    if current.workspace_id is None:
        raise HTTPException(409, "create a workspace first")
    return {
        "plan_status": _effective_status(current.plan_status, current.trial_ends_at),
        "trial_ends_at": (
            current.trial_ends_at.isoformat() if current.trial_ends_at else None
        ),
        "days_left": _days_left(current.plan_status, current.trial_ends_at),
        "writable": auth.workspace_writable(current.plan_status, current.trial_ends_at),
    }


@router.post("/checkout")
async def billing_checkout(
    current: auth.AuthContext = Depends(auth.require_role("owner")),
) -> Dict[str, Any]:
    """Activate the paid plan. STUB: flips the workspace to active immediately and
    returns a Stripe-shaped contract ({activated, url}). Owner-only, and NOT
    write-gated — an expired workspace must still be able to pay. Chunk 5 swaps
    the body to create a real Stripe Checkout session and return its url.

    Gated on BILLING_STUB_CHECKOUT (default off) because activating a paid plan
    without payment is a free-plan giveaway on any public deployment."""
    if not config.BILLING_STUB_CHECKOUT:
        raise HTTPException(
            501,
            "billing is not configured on this instance — no payment provider is wired up",
        )
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE workspaces SET plan='team', plan_status='active' WHERE id=$1",
                current.workspace_id,
            )
            await auth.audit(conn, "billing_checkout", current.workspace_id,
                             current.user_id, "workspace", current.workspace_id,
                             {"stub": True})
    return {"activated": True, "url": None}
