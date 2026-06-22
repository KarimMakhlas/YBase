"""Live Slack ingestion via the Events API.

Point a Slack app's Event Subscriptions at POST /api/integrations/slack/events
(scopes: message.channels). Messages are buffered per-thread in slack_events;
once a thread has been quiet for SLACK_THREAD_QUIET_S the worker's integration
tick rolls it up into one document (threads are the natural decision unit) and
ingests it through the normal pipeline. Substance filter: threads shorter than
SLACK_MIN_THREAD_CHARS are dropped, mirroring import_slack.py.

Set SLACK_SIGNING_SECRET to enable the endpoint. Events are accepted only for
Slack teams installed through Sources and channels selected by an admin.
"""

import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core import config, db

log = logging.getLogger("ybase.slack")

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_BARE_LINK_RE = re.compile(r"<(https?://[^>]+)>")


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str,
                     now: Optional[float] = None) -> bool:
    """Slack v0 signing check with a 5-minute replay window."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = float(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > 300:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def clean_text(text: str) -> str:
    text = _MENTION_RE.sub(lambda m: f"@{m.group(1)}", text or "")
    text = _LINK_RE.sub(lambda m: m.group(2), text)
    text = _BARE_LINK_RE.sub(lambda m: m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def wanted_event(event: Dict[str, Any]) -> bool:
    if event.get("type") != "message":
        return False
    # edits, joins, bot chatter etc. carry a subtype — skip them
    if event.get("subtype"):
        return False
    if not (event.get("text") or "").strip():
        return False
    return True


def thread_document(
    channel: str,
    messages: List[Dict[str, Any]],
    *,
    source_connection_id: Optional[int] = None,
    source_stream_id: Optional[int] = None,
    external_ref: Optional[str] = None,
    stream_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Roll one thread's buffered messages into an ingestable document."""
    lines = [f"{m['user_id'] or 'unknown'}: {m['text']}" for m in messages]
    text = "\n\n".join(lines)
    first = messages[0]
    head = (first["text"] or "").split("\n")[0][:70]
    name = stream_name or channel
    return {
        "source": "slack",
        "title": f"#{name} thread: {head}",
        "text": text,
        "author": first["user_id"],
        "created_at": first["event_at"].isoformat()
        if isinstance(first["event_at"], datetime) else first["event_at"],
        "tags": [],
        "source_connection_id": source_connection_id,
        "source_stream_id": source_stream_id,
        "external_ref": external_ref,
    }


async def store_event(event: Dict[str, Any], team_id: Optional[str] = None) -> int:
    if not team_id or not wanted_event(event):
        return 0
    channel = event.get("channel", "")
    if not channel:
        return 0
    ts = event.get("ts", "")
    thread_key = event.get("thread_ts") or ts
    try:
        event_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        event_at = datetime.now(timezone.utc)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        targets = await conn.fetch(
            "SELECT c.workspace_id, c.id AS connection_id, s.id AS stream_id "
            "FROM source_connections c "
            "JOIN source_streams s ON s.connection_id=c.id "
            "WHERE c.provider='slack' AND c.status='connected' "
            "AND c.external_workspace_id=$1 AND s.external_id=$2 AND s.selected",
            team_id, channel,
        )
        stored = 0
        for target in targets:
            status = await conn.execute(
                "INSERT INTO slack_events(workspace_id, source_connection_id, source_stream_id, "
                "channel, thread_key, ts, user_id, text, event_at) "
                "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT DO NOTHING",
                target["workspace_id"], target["connection_id"], target["stream_id"],
                channel, thread_key, ts, event.get("user"),
                clean_text(event.get("text", "")), event_at,
            )
            if status.endswith(" 1"):
                stored += 1
    return stored


async def rollup_quiet_threads() -> int:
    """Ingest threads that have gone quiet. Called from the worker tick."""
    if not config.SLACK_SIGNING_SECRET:
        return 0
    from app.domains.documents.ingestion import IngestRequest, ingest_document  # lazy: ingest -> worker -> slack

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        threads = await conn.fetch(
            "SELECT e.workspace_id, e.source_connection_id, e.source_stream_id, e.channel, "
            "e.thread_key, c.external_workspace_id, s.name AS stream_name "
            "FROM slack_events e "
            "JOIN source_connections c ON c.id=e.source_connection_id "
            "JOIN source_streams s ON s.id=e.source_stream_id "
            "WHERE NOT e.consumed "
            "GROUP BY e.workspace_id, e.source_connection_id, e.source_stream_id, e.channel, "
            "e.thread_key, c.external_workspace_id, s.name "
            "HAVING max(event_at) < now() - ($1 || ' seconds')::interval",
            str(config.SLACK_THREAD_QUIET_S),
        )
    ingested = 0
    for t in threads:
        async with pool.acquire() as conn:
            messages = [dict(r) for r in await conn.fetch(
                "SELECT user_id, text, event_at FROM slack_events "
                "WHERE workspace_id=$1 AND source_connection_id=$2 AND source_stream_id=$3 "
                "AND channel=$4 AND thread_key=$5 AND NOT consumed ORDER BY ts",
                t["workspace_id"], t["source_connection_id"], t["source_stream_id"],
                t["channel"], t["thread_key"],
            )]
        if not messages:
            continue
        doc = thread_document(
            t["channel"],
            messages,
            source_connection_id=t["source_connection_id"],
            source_stream_id=t["source_stream_id"],
            external_ref=(
                f"slack:{t['external_workspace_id']}:{t['channel']}:{t['thread_key']}"
            ),
            stream_name=t["stream_name"],
        )
        substantial = len(doc["text"]) >= config.SLACK_MIN_THREAD_CHARS
        if substantial:
            await ingest_document(IngestRequest(**doc), workspace_id=t["workspace_id"])
            ingested += 1
            log.info("ingested slack thread %s/%s (%d messages)",
                     t["channel"], t["thread_key"], len(messages))
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE slack_events SET consumed=TRUE "
                "WHERE workspace_id=$1 AND source_connection_id=$2 AND source_stream_id=$3 "
                "AND channel=$4 AND thread_key=$5",
                t["workspace_id"], t["source_connection_id"], t["source_stream_id"],
                t["channel"], t["thread_key"],
            )
    return ingested
