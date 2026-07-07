"""YBase MCP server: exposes the company memory layer to MCP clients
(Claude Code, Cursor, any MCP-compatible agent) as tools wrapping the
YBase agent API — reads plus a curated write path (propose_decision). Thin by design — all intelligence lives server-side; this
process just speaks stdio MCP on one end and authenticated HTTPS on the other.

Configuration (environment):
  YBASE_BASE_URL  e.g. https://ybase.example.com (no trailing slash needed)
  YBASE_API_KEY   a workspace API key minted at Settings → API keys (ybk_...)
"""

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ybase")

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


def _config() -> tuple[str, str]:
    base = (os.environ.get("YBASE_BASE_URL") or "").rstrip("/")
    key = os.environ.get("YBASE_API_KEY") or ""
    if not base or not key:
        raise RuntimeError(
            "YBASE_BASE_URL and YBASE_API_KEY must be set in the MCP server's "
            "environment (mint a key at Settings → API keys in YBase)."
        )
    return base, key


async def _request(method: str, path: str, *, json_body: Optional[dict] = None,
                   params: Optional[dict] = None) -> Any:
    base, key = _config()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        res = await cx.request(
            method, f"{base}{path}",
            headers={"Authorization": f"Bearer {key}"},
            json=json_body, params=params,
        )
    if res.status_code == 401:
        raise RuntimeError("YBase rejected the API key (revoked or wrong workspace?)")
    if res.status_code == 429:
        raise RuntimeError("YBase agent rate limit hit — slow down and retry shortly")
    res.raise_for_status()
    return res.json()


def _pretty(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


@mcp.tool()
async def ask_ybase(question: str) -> str:
    """Ask the company's institutional memory a question and get an
    evidence-backed answer with citations, confidence, and warnings.

    Use this to understand WHY something is the way it is before changing it:
    "Why do we authorize payment before booking creation?", "What did we decide
    about database sharding?". The `warnings` field lists decisions in this
    area that were later reversed or revisited — never build on those.
    """
    return _pretty(await _request("POST", "/api/agent/ask",
                                  json_body={"question": question}))


@mcp.tool()
async def get_context_for_task(task: str, topics: Optional[list[str]] = None) -> str:
    """Get the pre-action briefing for a task you are about to perform:
    relevant past decisions (with status and confidence), warnings about
    reversed/revisited decisions, open questions, and the people involved.

    Call this BEFORE modifying code, writing a design, opening a PR, or
    making a recommendation in an area you haven't touched this session.
    Describe the task in plain language, e.g. "refactor the booking
    cancellation flow". Fast (no LLM call server-side) — cheap to call once
    per task. Optional `topics` narrows the search (e.g. ["payments"]).
    """
    body: dict = {"task": task}
    if topics:
        body["topics"] = topics
    return _pretty(await _request("POST", "/api/agent/context", json_body=body))


@mcp.tool()
async def search_memory(query: str, kind: Optional[str] = None,
                        status: Optional[str] = None) -> str:
    """Search company memory nodes by name/summary text. Returns matching
    decisions, questions, entities, and topics with status and confidence.

    Use to locate a specific known thing ("MongoDB decision", "Q3 pricing").
    For open-ended questions use ask_ybase instead. Optional filters:
    kind = decision | question | entity | topic;
    status = decided | proposed | revisited | reversed | open | resolved.
    """
    params: dict = {"q": query}
    if kind:
        params["kind"] = kind
    if status:
        params["status"] = status
    return _pretty(await _request("GET", "/api/agent/search", params=params))


@mcp.tool()
async def get_decision(node_id: int) -> str:
    """Fetch one decision's full evidence chain: reasoning, alternatives
    considered, who made it, source documents, verbatim evidence excerpts,
    and supersession links (what it supersedes / what superseded it).

    Use after search_memory or ask_ybase returns a decision node id you need
    to inspect in depth — e.g. before proposing a change that touches it.
    A non-empty `superseded_by` means this decision is NOT current.
    """
    return _pretty(await _request("GET", f"/api/agent/decisions/{node_id}"))


@mcp.tool()
async def context_for_file(path: str, repo: Optional[str] = None) -> str:
    """Get the decision history relevant to a specific file you are about to
    read or modify — call this when opening a file in an unfamiliar area.

    Pass the repo-relative path (e.g. "src/billing/charge.ts"); the server
    mines it for domain terms and returns relevant past decisions, warnings
    about reversed/revisited ones, and open questions. `derived_terms` in the
    response shows which words from the path drove the match. Fast (no LLM
    call) — cheap to call once per file. Optional `repo` adds the repository
    name as extra context.
    """
    body: dict = {"path": path}
    if repo:
        body["repo"] = repo
    return _pretty(await _request("POST", "/api/agent/context-for-file", json_body=body))


@mcp.tool()
async def propose_decision(label: str, summary: str, topics: list[str],
                           kind: str = "decision",
                           status: Optional[str] = None,
                           made_by: Optional[list[str]] = None) -> str:
    """Propose a new decision (or open question) for the company's memory.
    The proposal does NOT become live memory — it queues for a human curator
    to approve or reject, and only approval creates a memory node.

    Use this when you and the user just made a real decision worth
    remembering ("we chose X over Y because Z"), or discovered an open
    question the team must resolve (kind="question"). Write the label as a
    short decision title and put the what + reasoning in `summary`. `topics`
    are short lowercase tags (e.g. ["billing", "retries"]) — reuse existing
    topic names from search_memory when possible. Check the returned
    `warnings`: an existing node with the same label means approval will
    merge into it. Track the outcome later with check_proposal.
    """
    body: dict = {"kind": kind, "label": label, "summary": summary, "topics": topics}
    if status:
        body["status"] = status
    if made_by:
        body["data"] = {"made_by": made_by}
    return _pretty(await _request("POST", "/api/agent/propose", json_body=body))


@mcp.tool()
async def check_proposal(proposal_id: int) -> str:
    """Check what happened to a proposal submitted with propose_decision:
    still pending, approved (includes the created memory node id), or
    rejected (includes the curator's note explaining why).
    """
    return _pretty(await _request("GET", f"/api/agent/proposals/{proposal_id}"))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
