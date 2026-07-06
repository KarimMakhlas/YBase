"""Live Slack ingestion (Events API)."""

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.core import config
from app.core.ratelimit import slack_events_limiter
from app.domains.connectors.slack import events as slack

router = APIRouter(prefix="/api", tags=["integrations"])


@router.post("/integrations/slack/events")
async def slack_events(request: Request) -> Dict[str, Any]:
    if not config.SLACK_SIGNING_SECRET:
        raise HTTPException(404, "slack integration not configured")
    body = await request.body()
    if not slack.verify_signature(
        config.SLACK_SIGNING_SECRET,
        request.headers.get("x-slack-request-timestamp", ""),
        body,
        request.headers.get("x-slack-signature", ""),
    ):
        raise HTTPException(401, "bad slack signature")
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if payload.get("type") == "event_callback":
        team_id = payload.get("team_id") or payload.get("team")
        # Per-team budget: drop quietly (200) when a workspace floods us, so one
        # noisy tenant can't exhaust the DB pool for everyone. Slack treats 200
        # as delivered and won't retry; a 429 would.
        if not await slack_events_limiter.allow(team_id or "unknown"):
            return {"ok": True, "throttled": True}
        stored = await slack.store_event(
            payload.get("event") or {},
            team_id=team_id,
        )
        return {"ok": True, "stored": stored}
    return {"ok": True}
