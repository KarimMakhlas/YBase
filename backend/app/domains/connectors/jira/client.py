"""Jira (Atlassian Cloud) connector: OAuth 3LO, REST v3, and issue sync.

Maps onto the generic connector model: a Jira site is a source_connection
(external_workspace_id = cloud id), each project is a source_stream, and each
issue (plus its comments) becomes a document. Access tokens expire hourly, so
every API call goes through valid_access_token(), which refreshes and persists
the rotating refresh token when needed.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
API_BASE = "https://api.atlassian.com"
SCOPES = "read:jira-work read:jira-user offline_access"

ISSUE_FIELDS = [
    "summary", "description", "comment", "reporter", "assignee",
    "status", "issuetype", "priority", "labels", "created", "updated",
]


class JiraRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class JiraAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.JIRA_CLIENT_ID and config.JIRA_CLIENT_SECRET and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.JIRA_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/jira/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "audience": "api.atlassian.com",
        "client_id": config.JIRA_CLIENT_ID,
        "scope": SCOPES,
        "redirect_uri": redirect_uri(),
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    })
    return f"{AUTHORIZE_URL}?{params}"


# ---- OAuth token endpoints ----

async def _token_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(TOKEN_URL, json=payload)
    res.raise_for_status()
    return res.json()


async def exchange_code(code: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "authorization_code",
        "client_id": config.JIRA_CLIENT_ID,
        "client_secret": config.JIRA_CLIENT_SECRET,
        "code": code,
        "redirect_uri": redirect_uri(),
    })


async def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "refresh_token",
        "client_id": config.JIRA_CLIENT_ID,
        "client_secret": config.JIRA_CLIENT_SECRET,
        "refresh_token": refresh_token,
    })


async def accessible_resources(access_token: str) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.get(
            f"{API_BASE}/oauth/token/accessible-resources",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    res.raise_for_status()
    return res.json()


async def valid_access_token(conn, connection) -> str:
    """Return a non-expired access token for the connection, refreshing and
    persisting the rotating refresh token when the current one is near expiry."""
    expires = connection["token_expires_at"]
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    if connection["access_token_enc"] and expires is not None and expires > soon:
        return crypto.decrypt_secret(connection["access_token_enc"])
    if not connection["refresh_token_enc"]:
        raise JiraAPIError("missing refresh token; reconnect Jira")
    payload = await refresh_tokens(crypto.decrypt_secret(connection["refresh_token_enc"]))
    access = payload["access_token"]
    new_refresh = payload.get("refresh_token") or crypto.decrypt_secret(connection["refresh_token_enc"])
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
    await conn.execute(
        "UPDATE source_connections SET access_token_enc=$2, refresh_token_enc=$3, "
        "token_expires_at=$4, updated_at=now() WHERE id=$1",
        connection["id"], crypto.encrypt_secret(access),
        crypto.encrypt_secret(new_refresh), new_expiry,
    )
    return access


# ---- Jira REST v3 (per cloud site) ----

async def _request(
    cloud_id: str, token: str, method: str, path: str,
    params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{API_BASE}/ex/jira/{cloud_id}{path}"
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.request(
            method, url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params, json=json,
        )
    if res.status_code == 429:
        raise JiraRateLimit(int(res.headers.get("Retry-After", "60")))
    if res.status_code >= 400:
        raise JiraAPIError(f"jira {method} {path} -> {res.status_code}: {res.text[:300]}")
    return res.json()


async def search_projects(cloud_id: str, token: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    start = 0
    while True:
        data = await _request(
            cloud_id, token, "GET", "/rest/api/3/project/search",
            params={"startAt": start, "maxResults": 50, "orderBy": "name"},
        )
        out.extend(data.get("values", []))
        if data.get("isLast", True) or not data.get("values"):
            break
        start += len(data["values"])
    return out


async def search_issues(
    cloud_id: str, token: str, jql: str, next_page_token: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"jql": jql, "maxResults": 50, "fields": ISSUE_FIELDS}
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return await _request(cloud_id, token, "POST", "/rest/api/3/search/jql", json=body)


# ---- Atlassian Document Format -> plain text ----

def adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or any node) to readable plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return "@" + (node.get("attrs", {}) or {}).get("text", "user")
    if kind == "emoji":
        return (node.get("attrs", {}) or {}).get("text", "")
    inner = adf_to_text(node.get("content"))
    if kind in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner + "\n"
    if kind == "listItem":
        return "- " + inner
    return inner


def _iso(value: Optional[str]) -> Optional[str]:
    """Normalize Jira timestamps (2026-01-15T09:30:00.000+0000) to RFC3339 with
    a colon in the offset so ingest._parse_date accepts them on Python 3.9."""
    if not value:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?([+-]\d{2}):?(\d{2})", value)
    return f"{m.group(1)}{m.group(2)}:{m.group(3)}" if m else value


def issue_to_doc(connection, stream, issue: Dict[str, Any]) -> Optional[IngestRequest]:
    key = issue.get("key")
    fields = issue.get("fields") or {}
    summary = (fields.get("summary") or "").strip()
    if not key:
        return None

    def _name(obj: Any) -> str:
        return (obj or {}).get("displayName") or (obj or {}).get("name") or ""

    meta_bits = []
    if (fields.get("issuetype") or {}).get("name"):
        meta_bits.append(f"Type: {fields['issuetype']['name']}")
    if (fields.get("status") or {}).get("name"):
        meta_bits.append(f"Status: {fields['status']['name']}")
    if (fields.get("priority") or {}).get("name"):
        meta_bits.append(f"Priority: {fields['priority']['name']}")
    reporter = _name(fields.get("reporter"))
    if reporter:
        meta_bits.append(f"Reporter: {reporter}")
    assignee = _name(fields.get("assignee"))
    if assignee:
        meta_bits.append(f"Assignee: {assignee}")
    labels = fields.get("labels") or []

    lines = [f"[{key}] {summary}"]
    if meta_bits:
        lines.append(" | ".join(meta_bits))
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    description = adf_to_text(fields.get("description")).strip()
    if description:
        lines.append("\n" + description)
    comments = ((fields.get("comment") or {}).get("comments")) or []
    if comments:
        lines.append("\n--- Comments ---")
        for c in comments:
            author = _name(c.get("author"))
            when = (_iso(c.get("created")) or "")[:10]
            body = adf_to_text(c.get("body")).strip()
            if body:
                lines.append(f"{author or 'unknown'} ({when}): {body}")

    text = "\n".join(lines).strip()
    return IngestRequest(
        source="jira",
        title=f"[{key}] {summary}"[:200] or key,
        text=text,
        author=reporter or None,
        created_at=_iso(fields.get("created")),
        tags=[stream["name"], key.split("-")[0]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        # Stable per issue: re-syncs skip already-imported issues (matches Slack).
        external_ref=f"jira:{connection['external_workspace_id']}:{key}",
    )


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    projects = await search_projects(connection["external_workspace_id"], token)
    rows: List[Dict[str, Any]] = []
    for p in projects:
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'jira', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], p.get("key", p.get("id")),
            p.get("name", p.get("key", "Project")),
            {"project_id": p.get("id"), "project_type": p.get("projectTypeKey"),
             "lead": (p.get("lead") or {}).get("displayName")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_project(
    cloud_id: str, token: str, connection, stream, days: int, job_id: int,
) -> Tuple[int, int]:
    jql = (
        f'project = "{stream["external_id"]}" AND updated >= "-{days}d" '
        "ORDER BY updated ASC"
    )
    created = duplicate = 0
    seen = 0
    next_token: Optional[str] = None
    pool = await db.get_pool()
    while seen < config.JIRA_MAX_ISSUES_PER_PROJECT:
        data = await search_issues(cloud_id, token, jql, next_token)
        issues = data.get("issues") or []
        for issue in issues:
            doc = issue_to_doc(connection, stream, issue)
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
                         "issues_seen": seen},
            )
        next_token = data.get("nextPageToken")
        if data.get("isLast", next_token is None) or not next_token or not issues:
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
        if connection is None or connection["provider"] != "jira":
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
    days = int((job["state"] or {}).get("days", 90))
    cloud_id = connection["external_workspace_id"]
    stats = {"documents": 0, "duplicates": 0, "streams": 0}
    current_stream_id: Optional[int] = None
    try:
        async with pool.acquire() as conn:
            token = await valid_access_token(conn, connection)
        for stream in streams:
            current_stream_id = stream["id"]
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='syncing', last_error=NULL, updated_at=now() "
                    "WHERE id=$1", stream["id"],
                )
            created, duplicate = await _sync_project(cloud_id, token, connection, stream, days, job_id)
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
    except JiraRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Jira rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id, f"Jira rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Jira rate limited sync; retry after {e.retry_after}s",
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
