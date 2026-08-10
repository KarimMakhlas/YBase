"""Linear connector: OAuth2, GraphQL API, and issue sync.

Maps onto the generic connector model: a Linear workspace (organization) is a
source_connection, each team is a source_stream, and each issue (plus its
comments) becomes a document. Access tokens expire in 24h, so every API call
goes through valid_access_token(), which refreshes and persists the rotating
refresh token when needed — the same shape as jira/client.py, minus Jira's
cloud-id resolution step (a Linear token is already scoped to one workspace).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.core import config, crypto, db
from app.domains.auth import service as auth
from app.domains.connectors import stream_lookback_days
from app.domains.documents.ingestion import IngestRequest, ingest_document

AUTHORIZE_URL = "https://linear.app/oauth/authorize"
TOKEN_URL = "https://api.linear.app/oauth/token"
GRAPHQL_URL = "https://api.linear.app/graphql"
SCOPES = "read"

ISSUES_QUERY = """
query IssuesForTeam($teamId: String!, $after: String, $updatedSince: DateTimeOrDuration) {
  team(id: $teamId) {
    issues(first: 50, after: $after, orderBy: updatedAt,
           filter: {updatedAt: {gte: $updatedSince}}) {
      nodes {
        id
        identifier
        title
        description
        priority
        createdAt
        updatedAt
        state { name }
        assignee { name }
        creator { name }
        labels { nodes { name } }
        comments(first: 50) {
          nodes { body createdAt user { name } }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class LinearRateLimit(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class LinearAPIError(Exception):
    pass


def configured() -> bool:
    return bool(
        config.LINEAR_CLIENT_ID and config.LINEAR_CLIENT_SECRET and config.CONNECTOR_SECRET_KEY
    )


def redirect_uri() -> str:
    return config.LINEAR_REDIRECT_BASE_URL.rstrip("/") + "/api/integrations/linear/oauth/callback"


def authorize_url(state: str) -> str:
    params = urlencode({
        "client_id": config.LINEAR_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{params}"


# ---- OAuth token endpoint (form-encoded, unlike Jira's JSON body) ----

async def _token_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as cx:
        res = await cx.post(TOKEN_URL, data=payload)
    res.raise_for_status()
    return res.json()


async def exchange_code(code: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "authorization_code",
        "client_id": config.LINEAR_CLIENT_ID,
        "client_secret": config.LINEAR_CLIENT_SECRET,
        "code": code,
        "redirect_uri": redirect_uri(),
    })


async def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    return await _token_request({
        "grant_type": "refresh_token",
        "client_id": config.LINEAR_CLIENT_ID,
        "client_secret": config.LINEAR_CLIENT_SECRET,
        "refresh_token": refresh_token,
    })


async def valid_access_token(conn, connection) -> str:
    """Return a non-expired access token for the connection, refreshing and
    persisting the rotating refresh token when the current one is near expiry."""
    expires = connection["token_expires_at"]
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    if connection["access_token_enc"] and expires is not None and expires > soon:
        return crypto.decrypt_secret(connection["access_token_enc"])
    if not connection["refresh_token_enc"]:
        raise LinearAPIError("missing refresh token; reconnect Linear")
    payload = await refresh_tokens(crypto.decrypt_secret(connection["refresh_token_enc"]))
    access = payload["access_token"]
    new_refresh = payload.get("refresh_token") or crypto.decrypt_secret(connection["refresh_token_enc"])
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 86400)))
    await conn.execute(
        "UPDATE source_connections SET access_token_enc=$2, refresh_token_enc=$3, "
        "token_expires_at=$4, updated_at=now() WHERE id=$1",
        connection["id"], crypto.encrypt_secret(access),
        crypto.encrypt_secret(new_refresh), new_expiry,
    )
    return access


# ---- GraphQL ----

async def _graphql(token: str, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=45) as cx:
        res = await cx.post(
            GRAPHQL_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )
    if res.status_code == 429:
        raise LinearRateLimit(int(res.headers.get("Retry-After", "60")))
    if res.status_code >= 400:
        raise LinearAPIError(f"linear graphql -> {res.status_code}: {res.text[:300]}")
    body = res.json()
    if body.get("errors"):
        raise LinearAPIError(str(body["errors"])[:300])
    return body["data"]


async def viewer_organization(token: str) -> Dict[str, Any]:
    data = await _graphql(token, "query { viewer { organization { id name } } }")
    return data["viewer"]["organization"]


async def list_teams(token: str) -> List[Dict[str, Any]]:
    data = await _graphql(token, "query { teams { nodes { id name key } } }")
    return data["teams"]["nodes"]


async def list_issues(
    token: str, team_id: str, updated_since: str, after: Optional[str] = None,
) -> Dict[str, Any]:
    data = await _graphql(token, ISSUES_QUERY, {
        "teamId": team_id, "after": after, "updatedSince": updated_since,
    })
    return data["team"]["issues"]


# ---- Document mapping ----

def issue_to_doc(connection, stream, issue: Dict[str, Any]) -> Optional[IngestRequest]:
    identifier = issue.get("identifier")
    title = (issue.get("title") or "").strip()
    if not identifier:
        return None

    def _name(obj: Any) -> str:
        return (obj or {}).get("name") or ""

    meta_bits = []
    if (issue.get("state") or {}).get("name"):
        meta_bits.append(f"Status: {issue['state']['name']}")
    creator = _name(issue.get("creator"))
    if creator:
        meta_bits.append(f"Creator: {creator}")
    assignee = _name(issue.get("assignee"))
    if assignee:
        meta_bits.append(f"Assignee: {assignee}")
    labels = [l.get("name") for l in (issue.get("labels") or {}).get("nodes", []) if l.get("name")]

    lines = [f"[{identifier}] {title}"]
    if meta_bits:
        lines.append(" | ".join(meta_bits))
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    description = (issue.get("description") or "").strip()
    if description:
        lines.append("\n" + description)
    comments = (issue.get("comments") or {}).get("nodes", [])
    if comments:
        lines.append("\n--- Comments ---")
        for c in comments:
            author = _name(c.get("user"))
            when = (c.get("createdAt") or "")[:10]
            body = (c.get("body") or "").strip()
            if body:
                lines.append(f"{author or 'unknown'} ({when}): {body}")

    text = "\n".join(lines).strip()
    return IngestRequest(
        source="linear",
        title=f"[{identifier}] {title}"[:200] or identifier,
        text=text,
        author=creator or None,
        created_at=issue.get("createdAt"),
        updated_at=issue.get("updatedAt"),
        tags=[stream["name"], identifier.split("-")[0]],
        source_connection_id=connection["id"],
        source_stream_id=stream["id"],
        # Stable per issue: re-syncs skip already-imported issues (matches Jira).
        external_ref=f"linear:{connection['external_workspace_id']}:{identifier}",
    )


async def refresh_streams(conn, connection, token: str) -> List[Dict[str, Any]]:
    teams = await list_teams(token)
    rows: List[Dict[str, Any]] = []
    for t in teams:
        stream = await conn.fetchrow(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, "
            "name, metadata) VALUES($1, $2, 'linear', $3, $4, $5) "
            "ON CONFLICT (connection_id, external_id) DO UPDATE SET "
            "name=EXCLUDED.name, metadata=source_streams.metadata || EXCLUDED.metadata, "
            "updated_at=now() RETURNING id, external_id, name, selected, status, "
            "last_synced_at, last_error, metadata",
            connection["workspace_id"], connection["id"], t["id"],
            t.get("name", t["id"]), {"team_key": t.get("key")},
        )
        rows.append(dict(stream))
    return rows


async def _sync_team(
    token: str, connection, stream, days: int, job_id: int,
) -> Tuple[int, int]:
    updated_since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created = duplicate = 0
    seen = 0
    after: Optional[str] = None
    pool = await db.get_pool()
    while seen < config.LINEAR_MAX_ISSUES_PER_TEAM:
        page = await list_issues(token, stream["external_id"], updated_since, after)
        issues = page.get("nodes") or []
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
        page_info = page.get("pageInfo") or {}
        after = page_info.get("endCursor")
        if not page_info.get("hasNextPage") or not issues:
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
        if connection is None or connection["provider"] != "linear":
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
            created, duplicate = await _sync_team(token, connection, stream, days, job_id)
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
    except LinearRateLimit as e:
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sync_jobs SET status='paused', stats=$2, error=$3, next_retry_at=$4, "
                "updated_at=now() WHERE id=$1",
                job_id, stats, f"Linear rate limit; retry after {e.retry_after}s", next_retry,
            )
            if current_stream_id is not None:
                await conn.execute(
                    "UPDATE source_streams SET status='paused', last_error=$2, updated_at=now() "
                    "WHERE id=$1", current_stream_id, f"Linear rate limit; retry after {e.retry_after}s",
                )
            await conn.execute(
                "UPDATE source_connections SET last_error=$2, updated_at=now() WHERE id=$1",
                connection["id"], f"Linear rate limited sync; retry after {e.retry_after}s",
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
