#!/usr/bin/env python3
"""Fetch issues + PRs from a public GitHub repo and ingest them into Whybase.

No GitHub OAuth App needed — works with the public GitHub REST API.
Set GITHUB_TOKEN (or --token) for 5000 req/hr instead of 60.

Usage:
    # Basic (public rate limit: 60 req/hr)
    python scripts/fetch_calcom_github.py

    # With a PAT (5000 req/hr, recommended for repos with many issues)
    GITHUB_TOKEN=ghp_... python scripts/fetch_calcom_github.py

    # Custom repo / window / limit
    python scripts/fetch_calcom_github.py --repo calcom/cal.com --days 90 --limit 150

Auth (Whybase admin account):
    export WHYBASE_EMAIL=you@example.com
    export WHYBASE_PASSWORD=yourpassword
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

BASE = os.environ.get("WHYBASE_API", "http://localhost:8100")
AUTH_EMAIL = os.environ.get("WHYBASE_EMAIL")
AUTH_PASSWORD = os.environ.get("WHYBASE_PASSWORD")
GITHUB_API = "https://api.github.com"

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest GitHub issues/PRs into Whybase")
    p.add_argument("--repo", default="calcom/cal.com", help="owner/repo (default: calcom/cal.com)")
    p.add_argument("--days", type=int, default=90, help="lookback window in days (default: 90)")
    p.add_argument("--limit", type=int, default=150, help="max items to ingest (default: 150)")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                   help="GitHub PAT (or set GITHUB_TOKEN env var)")
    return p.parse_args()


def gh_headers(token: str) -> Dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def next_link(res: httpx.Response) -> Optional[str]:
    m = re.search(r'<([^>]+)>;\s*rel="next"', res.headers.get("Link", ""))
    return m.group(1) if m else None


def gh_get(cx: httpx.Client, token: str, path: str,
           params: Optional[Dict[str, Any]] = None) -> httpx.Response:
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    res = cx.get(url, headers=gh_headers(token), params=params)
    remaining = res.headers.get("X-RateLimit-Remaining", "")
    if res.status_code == 403 and remaining == "0":
        reset = int(res.headers.get("X-RateLimit-Reset", "0"))
        wait = max(1, reset - int(datetime.now(timezone.utc).timestamp()))
        print(f"\n{YELLOW}GitHub rate limit hit. Sleeping {wait}s…{RESET}")
        time.sleep(wait + 2)
        res = cx.get(url, headers=gh_headers(token), params=params)
    res.raise_for_status()
    return res


def format_item(repo: str, issue: Dict[str, Any],
                comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    number = issue["number"]
    is_pr = "pull_request" in issue
    kind = "PR" if is_pr else "Issue"
    title = (issue.get("title") or "").strip()
    author = (issue.get("user") or {}).get("login") or ""
    labels = [lb["name"] for lb in (issue.get("labels") or []) if lb.get("name")]
    state = issue.get("state", "")

    lines = [f"{kind} #{number}: {title}"]
    meta = [f"State: {state}"]
    if author:
        meta.append(f"Author: {author}")
    if labels:
        meta.append("Labels: " + ", ".join(labels))
    lines.append(" | ".join(meta))
    body = (issue.get("body") or "").strip()
    if body:
        lines.append("\n" + body)
    for c in comments:
        cauthor = (c.get("user") or {}).get("login") or "unknown"
        cbody = (c.get("body") or "").strip()
        if cbody:
            lines.append(f"\n{cauthor}: {cbody}")

    repo_name = repo.split("/")[-1]
    return {
        "source": "github",
        "title": f"{repo}#{number}: {title}"[:200],
        "text": "\n".join(lines).strip(),
        "author": author or None,
        "created_at": issue.get("created_at"),
        "tags": [repo_name, "pr" if is_pr else "issue"],
        "external_ref": f"github:{repo}:{'pr' if is_pr else 'issue'}/{number}",
    }


def sb_login(cx: httpx.Client) -> None:
    if not AUTH_EMAIL or not AUTH_PASSWORD:
        print(f"{RED}Set WHYBASE_EMAIL and WHYBASE_PASSWORD before running.{RESET}")
        sys.exit(1)
    r = cx.post(f"{BASE}/api/auth/login", json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD})
    if r.status_code != 200:
        print(f"{RED}Login failed: {r.status_code} {r.text[:200]}{RESET}")
        sys.exit(1)


def sb_ingest(cx: httpx.Client, doc: Dict[str, Any]) -> tuple[int, bool]:
    r = cx.post(f"{BASE}/api/ingest", json=doc)
    r.raise_for_status()
    data = r.json()
    return data["document_id"], data["duplicate"]


def main() -> None:
    args = parse_args()
    repo = args.repo
    since_iso = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    print(f"{BOLD}Whybase — GitHub ingestion{RESET}")
    print(f"  repo  : {repo}")
    print(f"  since : {since_iso}  (last {args.days} days)")
    print(f"  limit : {args.limit} items")
    print(f"  auth  : {'PAT' if args.token else 'anonymous (60 req/hr)'}")
    if not args.token:
        print(f"  {YELLOW}Tip: set GITHUB_TOKEN for 5000 req/hr and much faster fetching.{RESET}")

    with httpx.Client(base_url=BASE, timeout=60, follow_redirects=True) as cx:
        sb_login(cx)
        print(f"{GREEN}Logged in to Whybase.{RESET}\n")

        with httpx.Client(timeout=45, follow_redirects=True) as gh:
            url: Optional[str] = None
            params: Optional[Dict[str, Any]] = {
                "state": "all", "since": since_iso,
                "per_page": 50, "sort": "updated", "direction": "asc",
            }
            seen = created = dupes = 0

            print(f"{BOLD}Fetching {repo}…{RESET}")
            while seen < args.limit:
                res = gh_get(gh, args.token,
                             url or f"/repos/{repo}/issues", params if not url else None)
                items = res.json()
                if not items:
                    break

                for issue in items:
                    if seen >= args.limit:
                        break
                    number = issue.get("number")
                    if number is None:
                        continue
                    is_pr = "pull_request" in issue
                    kind = "PR" if is_pr else "Issue"

                    # fetch comments (only when there are any)
                    comments: List[Dict[str, Any]] = []
                    if issue.get("comments", 0) > 0:
                        try:
                            cr = gh_get(gh, args.token,
                                        f"/repos/{repo}/issues/{number}/comments",
                                        {"per_page": 50})
                            comments = cr.json()
                        except httpx.HTTPStatusError:
                            pass

                    doc = format_item(repo, issue, comments)
                    label = f"{kind} #{number}: {issue.get('title', '')}"[:68]
                    print(f"  [{seen+1:>3}] {label:<68} ", end="", flush=True)

                    try:
                        doc_id, dup = sb_ingest(cx, doc)
                        if dup:
                            print(f"{DIM}skip (duplicate){RESET}")
                            dupes += 1
                        else:
                            print(f"{GREEN}✓ ingested (doc {doc_id}){RESET}")
                            created += 1
                    except httpx.HTTPStatusError as e:
                        print(f"{RED}✗ {e.response.status_code}{RESET}")

                    seen += 1

                url = next_link(res)
                params = None
                if not url:
                    break

    print(f"\n{GREEN}{BOLD}Done.{RESET} "
          f"{created} ingested, {dupes} skipped (already in memory), {seen} total fetched.")
    print(f"Open the Ops tab to watch memory formation: {BASE.replace('8100', '5173')}")


if __name__ == "__main__":
    main()
