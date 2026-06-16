"""Live Slack ingestion (Events API)."""

import json
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.core import config
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
        stored = await slack.store_event(
            payload.get("event") or {},
            team_id=payload.get("team_id") or payload.get("team"),
        )
        return {"ok": True, "stored": stored}
    return {"ok": True}
