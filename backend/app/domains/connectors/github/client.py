"""GitHub connector: OAuth App auth, REST, and issue/PR sync.

Maps onto the generic connector model: the authenticated GitHub account is a
source_connection, each repository is a source_stream, and each issue or pull
request (plus its comments) becomes a document. OAuth-App tokens don't expire,
so there's no refresh dance — and issue/PR bodies are Markdown, so no rich-text
parsing either.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_BASE = "https://api.github.com"
SCOPES = "repo read:org"


class GitHubRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class GitHubAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.GITHUB_CLIENT_ID and config.GITHUB_CLIENT_SECRET and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.GITHUB_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/github/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "client_id": config.GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{params}"


async def exchange_code(code: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(
            TOKEN_URL,
            headers={"Accept": "application/json"},
            json={
                "client_id": config.GITHUB_CLIENT_ID,
                "client_secret": config.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri(),
            },
        )
    res.raise_for_status()
    data = res.json()
    if "access_token" not in data:
        raise GitHubAPIError(data.get("error_description") or "no access token returned")
    return data


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get(token: str, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.get(url, headers=_headers(token), params=params)
    if res.status_code == 403 and res.headers.get("X-RateLimit-Remaining") == "0":
        reset = int(res.headers.get("X-RateLimit-Reset", "0"))
        retry = max(1, reset - int(datetime.now(timezone.utc).timestamp()))
        raise GitHubRateLimit(retry)
    if res.status_code >= 400:
        raise GitHubAPIError(f"github GET {path} -> {res.status_code}: {res.text[:300]}")
    return res


def _next_link(res: httpx.Response) -> Optional[str]:
    link = res.headers.get("Link", "")
    m = re.search(r'<([^>]+)>;\s*rel="next"', link)
    return m.group(1) if m else None


async def account(token: str) -> Dict[str, Any]:
    res = await _get(token, "/user")
    return res.json()


async def list_repos(token: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    url: Optional[str] = "/user/repos"
    params: Optional[Dict[str, Any]] = {
        "per_page": 100, "sort": "updated",
        "affiliation": "owner,collaborator,organization_member",
    }
    while url:
        res = await _get(token, url, params)
        out.extend(res.json())
        url = _next_link(res)
        params = None  # the next link already carries query params
    return out


async def list_issues(token: str, repo: str, since_iso: str, url: Optional[str] = None):
    """One page of issues+PRs (GitHub's issues endpoint returns both)."""
    if url:
        res = await _get(token, url)
    else:
        res = await _get(token, f"/repos/{repo}/issues", {
            "state": "all", "since": since_iso, "per_page": 50,
            "sort": "updated", "direction": "asc",
        })
    return res.json(), _next_link(res)


async def _comments(token: str, repo: str, number: int) -> List[Dict[str, Any]]:
    res = await _get(token, f"/repos/{repo}/issues/{number}/comments", {"per_page": 50})
    return res.json()


def _iso(value: Optional[str]) -> Optional[str]:
    return value  # GitHub already returns RFC3339 (2026-01-15T09:30:00Z)


async def issue_to_doc(token: str, connection, stream, issue: Dict[str, Any]) -> Optional[IngestRequest]:
    number = issue.get("number")
    if number is None:
        return None
    repo = stream["external_id"]
    is_pr = "pull_request" in issue
    kind = "PR" if is_pr else "Issue"
    title = (issue.get("title") or "").strip()
    author = (issue.get("user") or {}).get("login") or ""
    labels = [l.get("name") for l in (issue.get("labels") or []) if l.get("name")]

    lines = [f"{kind} #{number}: {title}"]
    meta = [f"State: {issue.get('state')}"]
    if author:
        meta.append(f"Author: {author}")
    if labels:
        meta.append("Labels: " + ", ".join(labels))
    lines.append(" | ".join(meta))
    body = (issue.get("body") or "").strip()
    if body:
        lines.append("\n" + body)
    if issue.get("comments"):
        for c in await _comments(token, repo, number):
            cauthor = (c.get("user") or {}).get("login") or "unknown"
            cbody = (c.get("body") or "").strip()
            if cbody:
                lines.append(f"\n{cauthor}: {cbody}")

    return IngestRequest(
        source="github",
        title=f"{repo}#{number}: {title}"[:200],
        text="\n".join(lines).strip(),
        author=author or None,
        created_at=_iso(issue.get("created_at")),
        tags=[repo.split("/")[-1], "pr" if is_pr else "issue"],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        external_ref=f"github:{repo}:{'pr' if is_pr else 'issue'}/{number}",
    )


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    repos = await list_repos(token)
    rows: List[Dict[str, Any]] = []
    for r in repos:
        full = r.get("full_name")
        if not full:
            continue
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'github', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], full, full,
            {"private": r.get("private"), "archived": r.get("archived"),
             "stars": r.get("stargazers_count")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_repo(token: str, connection, stream, days: int, job_id: int) -> Tuple[int, int]:
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = stream["external_id"]
    created = duplicate = seen = 0
    url: Optional[str] = None
    pool = await db.get_pool()
    while seen < config.GITHUB_MAX_ITEMS_PER_REPO:
        issues, next_url = await list_issues(token, repo, since_iso, url)
        if not issues:
            break
        for issue in issues:
            doc = await issue_to_doc(token, connection, stream, issue)
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
                         "items_seen": seen},
            )
        if not next_url:
            break
        url = next_url
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
        if connection is None or connection["provider"] != "github":
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
    token = crypto.decrypt_secret(connection["access_token_enc"])
    days = int((job["state"] or {}).get("days", 90))
    stats = {"documents": 0, "duplicates": 0, "streams": 0}
    current_stream_id: Optional[int] = None
    try:
        for stream in streams:
            current_stream_id = stream["id"]
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE source_streams SET status='syncing', last_error=NULL, updated_at=now() "
                    "WHERE id=$1", stream["id"],
                )
            created, duplicate = await _sync_repo(token, connection, stream, days, job_id)
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
    except GitHubRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"GitHub rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id, f"GitHub rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"GitHub rate limited sync; retry after {e.retry_after}s",
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
