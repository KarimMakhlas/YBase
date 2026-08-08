"""Discord connector: bot-token REST API and channel-message sync.

Maps onto the generic connector model: an installed guild (server) is a
source_connection, each text channel is a source_stream, and each day of
channel discussion becomes a digest document (modeled on the Slack connector's
loose-message digests). Auth is a static bot token from config — the OAuth
flow (bot scope) only picks which guild the bot is installed into; the token
itself never expires and is stored per-connection for uniformity.

Requires the Message Content privileged intent on the Discord app (fine
without verification under 100 guilds).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.documents.ingestion import IngestRequest, ingest_document

API_BASE = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
# View Channels (1024) + Read Message History (65536)
BOT_PERMISSIONS = 1024 + 65536
GUILD_TEXT = 0  # channel type
DISCORD_EPOCH_MS = 1420070400000


class DiscordRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class DiscordAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET
        and config.DISCORD_BOT_TOKEN and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.DISCORD_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/discord/oauth/callback"


def authorize_url(state: str) -> str:
    from urllib.parse import urlencode

    params = urlencode({
        "client_id": config.DISCORD_CLIENT_ID,
        "scope": "bot",
        "permissions": str(BOT_PERMISSIONS),
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{params}"


async def exchange_code(code: str) -> Dict[str, Any]:
    """Exchange the callback code. For bot-scope installs the response carries
    a `guild` object identifying where the bot was just installed."""
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(
            TOKEN_URL,
            data={
                "client_id": config.DISCORD_CLIENT_ID,
                "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
            },
        )
    res.raise_for_status()
    return res.json()


def _snowflake_after(dt: datetime) -> str:
    """Discord snowflake ids embed a timestamp — build one representing `dt`
    so the messages endpoint can paginate forward from a point in time."""
    ms = int(dt.timestamp() * 1000)
    return str(max(0, ms - DISCORD_EPOCH_MS) << 22)


async def _api(token: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bot {token}"},
            params=params or {},
        )
    if res.status_code == 429:
        try:
            retry = int(float(res.json().get("retry_after", 60))) + 1
        except Exception:
            retry = 60
        raise DiscordRateLimit(retry)
    if res.status_code >= 400:
        raise DiscordAPIError(f"discord GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def get_guild(token: str, guild_id: str) -> Dict[str, Any]:
    return await _api(token, f"/guilds/{guild_id}")


async def list_text_channels(token: str, guild_id: str) -> List[Dict[str, Any]]:
    channels = await _api(token, f"/guilds/{guild_id}/channels")
    return [c for c in channels if c.get("type") == GUILD_TEXT]


async def list_messages(token: str, channel_id: str, after: str) -> List[Dict[str, Any]]:
    """One page of up to 100 messages strictly after the given snowflake,
    oldest first (Discord returns ascending order when `after` is used)."""
    return await _api(
        token, f"/channels/{channel_id}/messages", {"after": after, "limit": 100}
    )


# ---- Document mapping (daily digests, like Slack's loose-message digests) ----

def _message_ok(m: Dict[str, Any]) -> bool:
    if (m.get("author") or {}).get("bot"):
        return False
    if m.get("type") not in (0, 19):  # DEFAULT and REPLY carry user prose
        return False
    return bool((m.get("content") or "").strip())


def _message_line(m: Dict[str, Any]) -> str:
    author = (m.get("author") or {}).get("username") or "unknown"
    return f"{author}: {(m.get('content') or '').strip()}"


def _day(m: Dict[str, Any]) -> str:
    return (m.get("timestamp") or "")[:10]


def digest_doc(connection, stream, day: str, messages: List[Dict[str, Any]]) -> Optional[IngestRequest]:
    messages = [m for m in messages if _message_ok(m)]
    if not messages:
        return None
    text = "\n\n".join(_message_line(m) for m in messages)
    if len(text) < config.DISCORD_MIN_DIGEST_CHARS:
        return None
    return IngestRequest(
        source="discord",
        title=f"#{stream['name']} — {day} discussion",
        text=text,
        author=(messages[0].get("author") or {}).get("username"),
        created_at=messages[0].get("timestamp"),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        external_ref=f"discord:{connection['external_workspace_id']}:{stream['external_id']}:digest:{day}",
    )


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    channels = await list_text_channels(token, connection["external_workspace_id"])
    rows: List[Dict[str, Any]] = []
    for ch in channels:
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'discord', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], ch["id"],
            ch.get("name", ch["id"]),
            {"topic": ch.get("topic"), "position": ch.get("position"), "nsfw": ch.get("nsfw")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_channel(
    token: str, connection, stream, days: int, job_id: int,
) -> Tuple[int, int]:
    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    after = _snowflake_after(oldest)
    messages: List[Dict[str, Any]] = []
    pool = await db.get_pool()
    while len(messages) < config.DISCORD_MAX_MESSAGES_PER_CHANNEL:
        page = await list_messages(token, stream["external_id"], after)
        if not page:
            break
        messages.extend(page)
        after = page[-1]["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                job_id, {"current_stream_id": stream["id"], "current_stream": stream["name"],
                         "messages_seen": len(messages)},
            )
        if len(page) < 100:
            break
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for m in messages:
        day = _day(m)
        if day:
            by_day.setdefault(day, []).append(m)
    created = duplicate = 0
    for day in sorted(by_day):
        doc = digest_doc(connection, stream, day, by_day[day])
        if doc is None:
            continue
        _, dup = await ingest_document(doc, workspace_id=connection["workspace_id"])
        duplicate += 1 if dup else 0
        created += 0 if dup else 1
    return created, duplicate


async def run_sync_job(job_id: int) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT * FROM sync_jobs WHERE id=$1", job_id)
        if job is None:
            return
        connection = await conn.fetchrow(
            "SELECT * FROM source_connections WHERE id=$1", job["connection_id"]
        )
        if connection is None or connection["provider"] != "discord":
            return
        streams = await conn.fetch(
            "SELECT * FROM source_streams WHERE connection_id=$1 AND selected ORDER BY name",
            connection["id"],
        )
        await conn.execute(
            "UPDATE sync_jobs SET status='running', started_at=COALESCE(started_at, now()), "
            "updated_at=now() WHERE id=$1",
            job_id,
        )
    job_state = job["state"] or {}
    token = crypto.decrypt_secret(connection["access_token_enc"])
    stats = {"documents": 0, "duplicates": 0, "streams": 0}
    current_stream_id: Optional[int] = None
    try:
        for stream in streams:
            current_stream_id = stream["id"]
            days = stream_lookback_days(job_state, stream["last_synced_at"])
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='syncing', last_error=NULL, updated_at=now() "
                    "WHERE id=$1", stream["id"],
                )
            created, duplicate = await _sync_channel(token, connection, stream, days, job_id)
            stats["documents"] += created
            stats["duplicates"] += duplicate
            stats["streams"] += 1
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='idle', last_synced_at=now(), "
                    "last_error=NULL, updated_at=now() WHERE id=$1", stream["id"],
                )
                await conn.execute(
                    "UPDATE sync_jobs SET stats=$2, updated_at=now() WHERE id=$1", job_id, stats,
                )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='complete', stats=$2, completed_at=now(), "
                "updated_at=now() WHERE id=$1", job_id, stats,
            )
            await conn.execute(
                "UPDATE source_connections SET last_sync_at=now(), last_error=NULL, updated_at=now() "
                "WHERE id=$1", connection["id"],
            )
            await auth.audit(conn, "sync_complete", connection["workspace_id"],
                             job["created_by"], "sync_job", job_id, stats)
    except DiscordRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Discord rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id,
                    f"Discord rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Discord rate limited sync; retry after {e.retry_after}s",
            )
    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='failed', stats=$2, error=$3, completed_at=now(), "
                "updated_at=now() WHERE id=$1", job_id, stats, str(e)[:1000],
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='failed', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id, str(e)[:500],
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], str(e)[:500],
            )
            await auth.audit(conn, "sync_failed", connection["workspace_id"],
                             job["created_by"], "sync_job", job_id, {"error": str(e)[:500], **stats})
