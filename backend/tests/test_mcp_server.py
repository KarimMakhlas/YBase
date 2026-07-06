"""YBase MCP server wrappers: config validation, request plumbing, and error
mapping — exercised against a mocked httpx transport, no network and no
running YBase. Skipped when the `mcp` SDK isn't installed (it's a dependency
of mcp-server/, not the backend)."""

import json
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("mcp")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp-server"))

from ybase_mcp import server  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("YBASE_BASE_URL", "https://ybase.test/")
    monkeypatch.setenv("YBASE_API_KEY", "ybk_test")


@pytest.fixture
def capture(monkeypatch):
    """Route the server's httpx calls into a canned handler, recording each
    request."""
    state = {"requests": [], "status": 200, "payload": {"ok": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return httpx.Response(state["status"], json=state["payload"])

    real_client = httpx.AsyncClient

    def patched_client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(server.httpx, "AsyncClient", patched_client)
    return state


def test_config_requires_env(monkeypatch):
    monkeypatch.delenv("YBASE_BASE_URL", raising=False)
    monkeypatch.delenv("YBASE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="YBASE_BASE_URL"):
        server._config()


async def test_ask_posts_question_with_auth(env, capture):
    capture["payload"] = {"answer": "because reasons", "confidence": "high"}
    out = await server.ask_ybase("Why MySQL?")
    req = capture["requests"][0]
    assert req.method == "POST"
    assert str(req.url) == "https://ybase.test/api/agent/ask"  # trailing slash stripped
    assert req.headers["Authorization"] == "Bearer ybk_test"
    assert json.loads(req.content) == {"question": "Why MySQL?"}
    assert json.loads(out)["answer"] == "because reasons"


async def test_context_and_search_and_decision_routes(env, capture):
    await server.get_context_for_task("refactor cancellation", topics=["payments"])
    await server.search_memory("MySQL", kind="decision", status="reversed")
    await server.get_decision(42)
    ctx_req, search_req, dec_req = capture["requests"]
    assert ctx_req.url.path == "/api/agent/context"
    assert json.loads(ctx_req.content) == {
        "task": "refactor cancellation", "topics": ["payments"]}
    assert search_req.url.path == "/api/agent/search"
    assert dict(search_req.url.params) == {
        "q": "MySQL", "kind": "decision", "status": "reversed"}
    assert dec_req.method == "GET"
    assert dec_req.url.path == "/api/agent/decisions/42"


async def test_error_mapping(env, capture):
    capture["status"] = 401
    with pytest.raises(RuntimeError, match="rejected the API key"):
        await server.search_memory("x")
    capture["status"] = 429
    with pytest.raises(RuntimeError, match="rate limit"):
        await server.search_memory("x")
    capture["status"] = 500
    with pytest.raises(httpx.HTTPStatusError):
        await server.search_memory("x")


async def test_tools_registered():
    # FastMCP must expose exactly the four tools, names stable (agents key on them)
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "ask_ybase", "get_context_for_task", "search_memory", "get_decision"}
