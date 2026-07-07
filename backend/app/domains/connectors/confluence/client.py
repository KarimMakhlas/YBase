"""Confluence (Atlassian Cloud) connector: OAuth 3LO, REST v2, and page sync.

Maps onto the generic connector model: a Confluence site is a source_connection
(external_workspace_id = cloud id), each space is a source_stream, and each
page becomes a document. Shares the Atlassian OAuth machinery and ADF parser
with Jira but uses its own OAuth app credentials, so Jira and Confluence are
independent connections. Page bodies are requested as atlas_doc_format (ADF as
a JSON string), which flattens through the same adf_to_text used for Jira.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core import config, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.connectors.atlassian import oauth as atlassian
from app.domains.connectors.atlassian.content import adf_to_text
from app.domains.documents.ingestion import IngestRequest, ingest_document

API_BASE = atlassian.API_BASE
SCOPES = "read:page:confluence read:space:confluence offline_access"


class ConfluenceRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class ConfluenceAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.CONFLUENCE_CLIENT_ID and config.CONFLUENCE_CLIENT_SECRET
        and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.CONFLUENCE_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/confluence/oauth/callback"


def authorize_url(state: str) -> str:
    return atlassian.authorize_url(config.CONFLUENCE_CLIENT_ID, SCOPES, redirect_uri(), state)


async def exchange_code(code: str) -> Dict[str, Any]:
    return await atlassian.exchange_code(
        config.CONFLUENCE_CLIENT_ID, config.CONFLUENCE_CLIENT_SECRET, code, redirect_uri()
    )


accessible_resources = atlassian.accessible_resources


async def valid_access_token(conn, connection) -> str:
    return await atlassian.valid_access_token(
        conn, connection, config.CONFLUENCE_CLIENT_ID, config.CONFLUENCE_CLIENT_SECRET,
        ConfluenceAPIError, "Confluence",
    )


# ---- Confluence REST v2 (per cloud site) ----

async def _request(cloud_id: str, token: str, path: str,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_BASE}/ex/confluence/{cloud_id}{path}"
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
        )
    if res.status_code == 429:
        raise ConfluenceRateLimit(int(res.headers.get("Retry-After", "60")))
    if res.status_code >= 400:
        raise ConfluenceAPIError(f"confluence GET {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def list_spaces(cloud_id: str, token: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    path: Optional[str] = "/wiki/api/v2/spaces"
    params: Optional[Dict[str, Any]] = {"limit": 100, "status": "current"}
    while path:
        data = await _request(cloud_id, token, path, params)
        out.extend(data.get("results", []))
        # v2 pagination: _links.next is a site-relative URL with the cursor baked in.
        path = (data.get("_links") or {}).get("next")
        params = None
    return out


async def list_pages(cloud_id: str, token: str, space_id: str,
                     path: Optional[str] = None) -> Dict[str, Any]:
    if path:
        return await _request(cloud_id, token, path)
    return await _request(
        cloud_id, token, f"/wiki/api/v2/spaces/{space_id}/pages",
        params={"limit": 50, "status": "current", "sort": "-modified-date",
                "body-format": "atlas_doc_format"},
    )


# ---- Document mapping ----

def page_to_doc(connection, stream, page: Dict[str, Any]) -> Optional[IngestRequest]:
    page_id = str(page.get("id") or "")
    title = (page.get("title") or "").strip()
    if not page_id:
        return None
    raw_body = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    body_text = ""
    if raw_body:
        try:
            body_text = adf_to_text(json.loads(raw_body)).strip()
        except (json.JSONDecodeError, TypeError):
            body_text = ""
    lines = [title]
    if body_text:
        lines.append("\n" + body_text)
    text = "\n".join(lines).strip()
    if len(text) <= len(title):  # empty page — title alone isn't a memory
        return None
    return IngestRequest(
        source="confluence",
        title=title[:200] or f"Page {page_id}",
        text=text,
        author=None,  # v2 pages carry authorId (account id) only; resolving names needs another scope
        created_at=page.get("createdAt"),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        # Stable per page: re-syncs skip already-imported pages (matches Jira issues).
        external_ref=f"confluence:{connection['external_workspace_id']}:{page_id}",
    )


def _page_modified_at(page: Dict[str, Any]) -> Optional[datetime]:
    raw = (page.get("version") or {}).get("createdAt") or page.get("createdAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    spaces = await list_spaces(connection["external_workspace_id"], token)
    rows: List[Dict[str, Any]] = []
    for s in spaces:
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'confluence', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], str(s["id"]),
            s.get("name", s.get("key", "Space")),
            {"space_key": s.get("key"), "space_type": s.get("type")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_space(
    cloud_id: str, token: str, connection, stream, days: int, job_id: int,
) -> Tuple[int, int]:
    """Walk the space's pages newest-modified-first, stopping at the window edge
    (the pages endpoint can't filter by date, but it can sort by it)."""
    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    created = duplicate = 0
    seen = 0
    next_path: Optional[str] = None
    pool = await db.get_pool()
    while seen < config.CONFLUENCE_MAX_PAGES_PER_SPACE:
        data = await list_pages(cloud_id, token, stream["external_id"], next_path)
        pages = data.get("results") or []
        stop = False
        for page in pages:
            modified = _page_modified_at(page)
            if modified is not None and modified < oldest:
                stop = True
                break
            doc = page_to_doc(connection, stream, page)
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
                         "pages_seen": seen},
            )
        next_path = (data.get("_links") or {}).get("next")
        if stop or not next_path or not pages:
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
        if connection is None or connection["provider"] != "confluence":
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
    cloud_id = connection["external_workspace_id"]
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
            created, duplicate = await _sync_space(cloud_id, token, connection, stream, days, job_id)
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
    except ConfluenceRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Confluence rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id,
                    f"Confluence rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Confluence rate limited sync; retry after {e.retry_after}s",
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
