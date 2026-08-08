"""Google Docs connector: OAuth2 (drive.readonly), Drive API, and doc sync.

Maps onto the generic connector model: the authorized Google account is a
source_connection (external_workspace_id = Drive permission id), a single
implicit stream covers all readable Docs (no folder picker in v1), and each
Google Doc becomes a document. Content comes from Drive's files.export as
text/plain, which sidesteps parsing the Docs API's structural-element tree.

Uses a separate OAuth client from the sign-in Google app so the
drive.readonly sensitive-scope verification never touches login. Google
refresh tokens don't rotate — the refresh response carries no new
refresh_token, and valid_access_token keeps the original.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
SCOPES = "https://www.googleapis.com/auth/drive.readonly"
DOC_MIME = "application/vnd.google-apps.document"
STREAM_EXTERNAL_ID = "documents"  # the single implicit stream
MAX_DOC_CHARS = 200_000  # keep one runaway doc from flooding ingestion


class GoogleDocsRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class GoogleDocsAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.GOOGLE_DOCS_CLIENT_ID and config.GOOGLE_DOCS_CLIENT_SECRET
        and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.GOOGLE_DOCS_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/googledocs/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "client_id": config.GOOGLE_DOCS_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",  # required for a refresh token
        "prompt": "consent",       # re-issue the refresh token on reconnect
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{params}"


async def _token_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(TOKEN_URL, data=payload)
    res.raise_for_status()
    return res.json()


async def exchange_code(code: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "authorization_code",
        "client_id": config.GOOGLE_DOCS_CLIENT_ID,
        "client_secret": config.GOOGLE_DOCS_CLIENT_SECRET,
        "code": code,
        "redirect_uri": redirect_uri(),
    })


async def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "refresh_token",
        "client_id": config.GOOGLE_DOCS_CLIENT_ID,
        "client_secret": config.GOOGLE_DOCS_CLIENT_SECRET,
        "refresh_token": refresh_token,
    })


async def valid_access_token(conn, connection) -> str:
    """Return a non-expired access token, refreshing when near expiry. Google
    refresh tokens don't rotate, so the stored one is kept as-is."""
    expires = connection["token_expires_at"]
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    if connection["access_token_enc"] and expires is not None and expires > soon:
        return crypto.decrypt_secret(connection["access_token_enc"])
    if not connection["refresh_token_enc"]:
        raise GoogleDocsAPIError("missing refresh token; reconnect Google Docs")
    payload = await refresh_tokens(crypto.decrypt_secret(connection["refresh_token_enc"]))
    access = payload["access_token"]
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    await conn.execute(
        "UPDATE source_connections SET access_token_enc=$2, token_expires_at=$3, "
        "updated_at=now() WHERE id=$1",
        connection["id"], crypto.encrypt_secret(access), new_expiry,
    )
    return access


# ---- Drive API ----

async def _get(token: str, url: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.get(url, headers={"Authorization": f"Bearer {token}"}, params=params or {})
    if res.status_code in (429, 403) and "rate" in res.text.lower():
        raise GoogleDocsRateLimit(int(res.headers.get("Retry-After", "60")))
    if res.status_code >= 400:
        raise GoogleDocsAPIError(f"drive GET -> {res.status_code}: {res.text[:300]}")
    return res


async def drive_user(token: str) -> Dict[str, Any]:
    res = await _get(token, f"{DRIVE_API}/about", {"fields": "user"})
    return res.json().get("user") or {}


async def list_docs(token: str, modified_after: str,
                    page_token: Optional[str] = None) -> Dict[str, Any]:
    params = {
        "q": f"mimeType='{DOC_MIME}' and trashed=false and modifiedTime > '{modified_after}'",
        "orderBy": "modifiedTime desc",
        "fields": "nextPageToken,files(id,name,createdTime,modifiedTime,owners(displayName))",
        "pageSize": 100,
    }
    if page_token:
        params["pageToken"] = page_token
    res = await _get(token, f"{DRIVE_API}/files", params)
    return res.json()


async def export_text(token: str, file_id: str) -> str:
    res = await _get(token, f"{DRIVE_API}/files/{file_id}/export", {"mimeType": "text/plain"})
    return res.text[:MAX_DOC_CHARS]


# ---- Document mapping ----

def file_to_doc(connection, stream, f: Dict[str, Any], text: str) -> Optional[IngestRequest]:
    file_id = f.get("id")
    name = (f.get("name") or "").strip() or "Untitled document"
    body = (text or "").strip()
    if not file_id or not body:
        return None
    owners = f.get("owners") or []
    return IngestRequest(
        source="googledocs",
        title=name[:200],
        text=f"{name}\n\n{body}",
        author=(owners[0].get("displayName") if owners else None),
        created_at=f.get("createdTime"),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        # Stable per file: re-syncs skip already-imported docs (matches Jira issues).
        external_ref=f"googledocs:{connection['external_workspace_id']}:{file_id}",
    )


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    """One implicit stream covering every readable Doc — v1 has no folder
    picker, so 'refresh' just upserts that row."""
    stream = await conn.fetchrow(
        "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
        "name, metadata) VALUES($1, $2, 'googledocs', $3, 'All Google Docs', $4) "
        "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
        "updated_at=now() RETURNING id, external_id, name, selected, status, "
        "last_synced_at, last_error, metadata",
        connection["workspace_id"], connection["id"], STREAM_EXTERNAL_ID, {},
    )
    return [dict(stream)]


async def _sync_docs(token: str, connection, stream, days: int, job_id: int) -> Tuple[int, int]:
    modified_after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    created = duplicate = 0
    seen = 0
    page_token: Optional[str] = None
    pool = await db.get_pool()
    while seen < config.GOOGLE_DOCS_MAX_DOCS:
        data = await list_docs(token, modified_after, page_token)
        files = data.get("files") or []
        for f in files:
            if seen >= config.GOOGLE_DOCS_MAX_DOCS:
                break
            try:
                text = await export_text(token, f["id"])
            except GoogleDocsAPIError:
                continue  # exports fail for some legacy/oversized docs; skip, don't abort
            doc = file_to_doc(connection, stream, f, text)
            if doc is None:
                continue
            _, dup = await ingest_document(doc, workspace_id=connection["workspace_id"])
            duplicate += 1 if dup else 0
            created += 0 if dup else 1
            seen += 1
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                job_id, {"current_stream_id": stream["id"], "current_stream": stream["name"],
                         "docs_seen": seen},
            )
        page_token = data.get("nextPageToken")
        if not page_token or not files:
            break
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
        if connection is None or connection["provider"] != "googledocs":
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
            created, duplicate = await _sync_docs(token, connection, stream, days, job_id)
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
    except GoogleDocsRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Google Docs rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id,
                    f"Google Docs rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Google Docs rate limited sync; retry after {e.retry_after}s",
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
