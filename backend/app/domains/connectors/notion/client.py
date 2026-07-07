"""Notion connector: OAuth (Basic-auth code exchange), REST API, and page sync.

Maps onto the generic connector model: a Notion workspace is a
source_connection (external_workspace_id = workspace id from the token
response), each top-level shared page or database is a source_stream, and each
page becomes a document (block tree flattened via content.tree_to_text; child
pages become their own documents within the same stream).

Notion tokens don't expire, so there's no refresh dance. "Top-level shared" is
derived from the search endpoint: a result whose parent is the workspace, or
whose parent page/database isn't itself accessible, is a root of what the user
shared with the integration.
"""

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.connectors.notion.content import page_title, tree_to_text
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
TOKEN_URL = "https://api.notion.com/v1/oauth/token"
API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_BLOCK_DEPTH = 8   # nested toggles/lists beyond this add noise, not memory
MAX_PAGE_DEPTH = 3    # child pages of child pages of a stream root


class NotionRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class NotionAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.NOTION_CLIENT_ID and config.NOTION_CLIENT_SECRET and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.NOTION_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/notion/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "client_id": config.NOTION_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "owner": "user",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{params}"


async def exchange_code(code: str) -> Dict[str, Any]:
    """Notion's exchange authenticates with HTTP Basic (client_id:client_secret),
    not credentials in the body like every other connector here."""
    basic = base64.b64encode(
        f"{config.NOTION_CLIENT_ID}:{config.NOTION_CLIENT_SECRET}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}"},
            json={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": redirect_uri()},
        )
    res.raise_for_status()
    return res.json()


# ---- REST ----

async def _request(token: str, method: str, path: str,
                   json: Optional[Dict[str, Any]] = None,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.request(
            method, f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
            json=json, params=params,
        )
    if res.status_code == 429:
        raise NotionRateLimit(int(float(res.headers.get("Retry-After", "60"))) + 1)
    if res.status_code >= 400:
        raise NotionAPIError(f"notion {method} {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def search_all(token: str) -> List[Dict[str, Any]]:
    """Everything (pages + databases) shared with the integration."""
    out: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        body: Dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await _request(token, "POST", "/search", json=body)
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


async def fetch_block_tree(token: str, block_id: str, budget: List[int],
                           depth: int = 0) -> List[Dict[str, Any]]:
    """Fetch a block's children recursively (paginated at every level),
    injecting each block's children under "__children" for tree_to_text.
    `budget` is a single-element mutable list counting down the per-page block
    allowance so one enormous page can't stall a sync."""
    if depth > MAX_BLOCK_DEPTH or budget[0] <= 0:
        return []
    blocks: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while budget[0] > 0:
        params: Dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = await _request(token, "GET", f"/blocks/{block_id}/children", params=params)
        page = data.get("results", [])
        for block in page:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            if block.get("has_children") and block.get("type") not in ("child_page", "child_database"):
                block["__children"] = await fetch_block_tree(token, block["id"], budget, depth + 1)
            blocks.append(block)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return blocks


async def query_database(token: str, database_id: str,
                         cursor: Optional[str] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"page_size": 100}
    if cursor:
        body["start_cursor"] = cursor
    return await _request(token, "POST", f"/databases/{database_id}/query", json=body)


# ---- Streams (top-level shared pages/databases) ----

def top_level_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Roots of the trees the user shared: parent is the workspace, or the
    parent page/database isn't itself accessible to the integration."""
    accessible = {r.get("id") for r in results}
    roots = []
    for r in results:
        parent = r.get("parent") or {}
        parent_id = parent.get("page_id") or parent.get("database_id") or parent.get("block_id")
        if parent.get("type") == "workspace" or (parent_id and parent_id not in accessible):
            roots.append(r)
    return roots


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    results = await search_all(token)
    rows: List[Dict[str, Any]] = []
    for r in top_level_results(results):
        kind = r.get("object")  # "page" | "database"
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'notion', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], r["id"],
            page_title(r)[:200], {"object": kind, "url": r.get("url")},
        )
        rows.append(dict(stream))
    return rows


# ---- Document mapping ----

def _last_edited(page: Dict[str, Any]) -> Optional[datetime]:
    raw = page.get("last_edited_time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def page_to_doc(connection, stream, page: Dict[str, Any], body_text: str) -> Optional[IngestRequest]:
    page_id = page.get("id")
    title = page_title(page)
    body = (body_text or "").strip()
    if not page_id or not body:
        return None
    return IngestRequest(
        source="notion",
        title=title[:200],
        text=f"{title}\n\n{body}",
        author=None,  # created_by carries a user id; resolving names needs another endpoint
        created_at=page.get("created_time"),
        tags=[stream["name"]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        # Stable per page: re-syncs skip already-imported pages.
        external_ref=f"notion:{connection['external_workspace_id']}:{page_id}",
    )


async def _ingest_page(token: str, connection, stream, page: Dict[str, Any]) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Fetch and ingest one page; returns (created, duplicate, child_page_blocks)."""
    budget = [config.NOTION_MAX_BLOCKS_PER_PAGE]
    tree = await fetch_block_tree(token, page["id"], budget)
    text = tree_to_text(tree)
    child_pages = [b for b in _walk(tree) if b.get("type") == "child_page"]
    doc = page_to_doc(connection, stream, page, text)
    if doc is None:
        return 0, 0, child_pages
    _, dup = await ingest_document(doc, workspace_id=connection["workspace_id"])
    return (0, 1, child_pages) if dup else (1, 0, child_pages)


def _walk(blocks: List[Dict[str, Any]]):
    for b in blocks or []:
        yield b
        yield from _walk(b.get("__children") or [])


async def _sync_stream(token: str, connection, stream, days: int, job_id: int) -> Tuple[int, int]:
    oldest = datetime.now(timezone.utc) - timedelta(days=days)
    created = duplicate = 0
    seen = 0
    pool = await db.get_pool()
    is_database = (stream["metadata"] or {}).get("object") == "database"

    # (page-like object, depth) work queue seeded from the stream root
    queue: List[Tuple[Dict[str, Any], int]] = []
    if is_database:
        cursor: Optional[str] = None
        while True:
            data = await query_database(token, stream["external_id"], cursor)
            for row in data.get("results", []):
                queue.append((row, 0))
            if not data.get("has_more") or len(queue) >= config.NOTION_MAX_PAGES_PER_STREAM:
                break
            cursor = data.get("next_cursor")
    else:
        page = await _request(token, "GET", f"/pages/{stream['external_id']}")
        queue.append((page, 0))

    while queue and seen < config.NOTION_MAX_PAGES_PER_STREAM:
        page, depth = queue.pop(0)
        modified = _last_edited(page)
        skip_content = modified is not None and modified < oldest
        if not skip_content:
            c, d, child_pages = await _ingest_page(token, connection, stream, page)
            created += c
            duplicate += d
            seen += 1
        else:
            # stale page itself, but children may have moved recently — still walk them
            budget = [config.NOTION_MAX_BLOCKS_PER_PAGE]
            tree = await fetch_block_tree(token, page["id"], budget)
            child_pages = [b for b in _walk(tree) if b.get("type") == "child_page"]
        if depth < MAX_PAGE_DEPTH:
            for child in child_pages:
                try:
                    child_page = await _request(token, "GET", f"/pages/{child['id']}")
                except NotionAPIError:
                    continue  # child_page blocks can be databases/inaccessible
                queue.append((child_page, depth + 1))
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET state = state || $2::jsonb, updated_at=now() WHERE id=$1",
                job_id, {"current_stream_id": stream["id"], "current_stream": stream["name"],
                         "pages_seen": seen},
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
        if connection is None or connection["provider"] != "notion":
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
            created, duplicate = await _sync_stream(token, connection, stream, days, job_id)
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
    except NotionRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Notion rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id,
                    f"Notion rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Notion rate limited sync; retry after {e.retry_after}s",
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
