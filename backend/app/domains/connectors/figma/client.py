"""Figma connector: OAuth2, REST API, and comment-thread sync.

Maps onto the generic connector model: the authorized Figma account is a
source_connection, each project in the user's team is a source_stream, and
each comment thread on a design file becomes a document (design discussion —
not file content).

Two Figma-specific quirks, both permanent API limitations:
- Token refresh is a separate endpoint (/v1/oauth/refresh), not the token
  endpoint reused with a refresh grant. Access tokens last ~90 days and the
  refresh token doesn't rotate.
- There is no API to list a user's teams. After connecting, the team id must
  be pasted manually from the Figma URL (figma.com/files/team/<id>/...) via
  the set-team route; it's stored in connection metadata and streams can only
  be discovered after that.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import base64
import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://www.figma.com/oauth"
TOKEN_URL = "https://api.figma.com/v1/oauth/token"
REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
API_BASE = "https://api.figma.com/v1"
SCOPES = "files:read"


class FigmaRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class FigmaAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.FIGMA_CLIENT_ID and config.FIGMA_CLIENT_SECRET and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.FIGMA_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/figma/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "client_id": config.FIGMA_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
        "response_type": "code",
    })
    return f"{AUTHORIZE_URL}?{params}"


def _basic_auth() -> str:
    return base64.b64encode(
        f"{config.FIGMA_CLIENT_ID}:{config.FIGMA_CLIENT_SECRET}".encode()
    ).decode()


async def exchange_code(code: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {_basic_auth()}"},
            data={"redirect_uri": redirect_uri(), "code": code,
                  "grant_type": "authorization_code"},
        )
    res.raise_for_status()
    return res.json()


async def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    """Figma's refresh lives on its own endpoint and doesn't rotate the
    refresh token — the response only carries a new access token."""
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(
            REFRESH_URL,
            headers={"Authorization": f"Basic {_basic_auth()}"},
            data={"refresh_token": refresh_token},
        )
    res.raise_for_status()
    return res.json()


async def valid_access_token(conn, connection) -> str:
    expires = connection["token_expires_at"]
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    if connection["access_token_enc"] and expires is not None and expires > soon:
        return crypto.decrypt_secret(connection["access_token_enc"])
    if not connection["refresh_token_enc"]:
        raise FigmaAPIError("missing refresh token; reconnect Figma")
    payload = await refresh_tokens(crypto.decrypt_secret(connection["refresh_token_enc"]))
    access = payload["access_token"]
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 7776000)))
    await conn.execute(
        "UPDATE source_connections SET access_token_enc=$2, token_expires_at=$3, "
        "updated_at=now() WHERE id=$1",
        connection["id"], crypto.encrypt_secret(access), new_expiry,
    )
    return access


# ---- REST ----

async def _api(token: str, path: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.get(f"{API_BASE}{path}", headers={"Authorization": f"Bearer {token}"})
    if res.status_code == 429:
        raise FigmaRateLimit(int(res.headers.get("Retry-After", "60")))
    if res.status_code >= 400:
        raise FigmaAPIError(f"figma GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def me(token: str) -> Dict[str, Any]:
    return await _api(token, "/me")


async def team_projects(token: str, team_id: str) -> Dict[str, Any]:
    return await _api(token, f"/teams/{team_id}/projects")


async def project_files(token: str, project_id: str) -> List[Dict[str, Any]]:
    data = await _api(token, f"/projects/{project_id}/files")
    return data.get("files") or []


async def file_comments(token: str, file_key: str) -> List[Dict[str, Any]]:
    data = await _api(token, f"/files/{file_key}/comments")
    return data.get("comments") or []


# ---- Document mapping (one doc per comment thread) ----

def group_threads(comments: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group a file's flat comment list into threads: a root comment (no
    parent_id) plus its replies, each thread ordered by creation time."""
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    roots: List[Dict[str, Any]] = []
    for c in comments:
        parent = c.get("parent_id") or ""
        if parent:
            by_parent.setdefault(parent, []).append(c)
        else:
            roots.append(c)
    threads = []
    for root in roots:
        replies = sorted(by_parent.get(root.get("id", ""), []),
                         key=lambda c: c.get("created_at") or "")
        threads.append([root] + replies)
    return threads


def _comment_line(c: Dict[str, Any]) -> str:
    handle = (c.get("user") or {}).get("handle") or "unknown"
    when = (c.get("created_at") or "")[:10]
    return f"{handle} ({when}): {(c.get('message') or '').strip()}"


def thread_doc(connection, stream, file: Dict[str, Any],
               thread: List[Dict[str, Any]]) -> Optional[IngestRequest]:
    thread = [c for c in thread if (c.get("message") or "").strip()]
    if not thread:
        return None
    root = thread[0]
    file_name = file.get("name") or file.get("key") or "File"
    head = (root.get("message") or "").strip().split("\n")[0][:70]
    text = f"Comments on {file_name}\n\n" + "\n\n".join(_comment_line(c) for c in thread)
    return IngestRequest(
        source="figma",
        title=f"{file_name}: {head}"[:200],
        text=text,
        author=(root.get("user") or {}).get("handle"),
        created_at=root.get("created_at"),
        updated_at=_latest_activity(thread) or root.get("created_at"),
        tags=[stream["name"], file_name[:60]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        external_ref=f"figma:{connection['external_workspace_id']}:{file.get('key')}:{root.get('id')}",
    )


def _latest_activity(thread: List[Dict[str, Any]]) -> str:
    return max((c.get("created_at") or "" for c in thread), default="")


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    """Projects in the manually-entered team. Without a team id there's nothing
    to discover — the UI prompts for it after connecting."""
    team_id = (connection["metadata"] or {}).get("team_id")
    if not team_id:
        return []
    data = await team_projects(token, str(team_id))
    rows: List[Dict[str, Any]] = []
    for p in data.get("projects") or []:
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'figma', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], str(p["id"]),
            p.get("name", str(p["id"])), {"team_id": team_id, "team_name": data.get("name")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_project(token: str, connection, stream, days: int, job_id: int) -> Tuple[int, int]:
    oldest_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = duplicate = 0
    files_seen = 0
    pool = await db.get_pool()
    files = await project_files(token, stream["external_id"])
    for f in files[: config.FIGMA_MAX_FILES_PER_PROJECT]:
        files_seen += 1
        try:
            comments = await file_comments(token, f["key"])
        except FigmaAPIError:
            continue  # deleted/no-access files shouldn't abort the project
        for thread in group_threads(comments):
            if _latest_activity(thread) < oldest_iso:
                continue  # thread went quiet before the window
            doc = thread_doc(connection, stream, f, thread)
            if doc is None:
                continue
            _, dup = await ingest_document(doc, workspace_id=connection["workspace_id"])
            duplicate += 1 if dup else 0
            created += 0 if dup else 1
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                job_id, {"current_stream_id": stream["id"], "current_stream": stream["name"],
                         "files_seen": files_seen},
            )
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
        if connection is None or connection["provider"] != "figma":
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
    stats = {"documents": 0, "duplicates": 0, "streams": 0}
    current_stream_id: Optional[int] = None
    try:
        async with pool.acquire() as conn:
            token = await valid_access_token(conn, connection)
        for stream in streams:
            current_stream_id = stream["id"]
            days = stream_lookback_days(job_state, stream["last_synced_at"])
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='syncing', last_error=NULL, updated_at=now() "
                    "WHERE id=$1", stream["id"],
                )
            created, duplicate = await _sync_project(token, connection, stream, days, job_id)
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
    except FigmaRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Figma rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id,
                    f"Figma rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Figma rate limited sync; retry after {e.retry_after}s",
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
