"""Usage accounting: payload parsers, contextvar attribution, formation
wiring, the /api/ops/usage endpoint, and retention pruning."""

from types import SimpleNamespace

from app.core import config, usage
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker
from app.providers import llm

from conftest import make_formation_result
from test_api_endpoints import _auth_client


def _req(**over):
    base = dict(source="meeting", title="Usage test", text="Some decision content.")
    base.update(over)
    return IngestRequest(**base)


# ── Pure parsers ─────────────────────────────────────────────────────────────


def test_usage_from_anthropic():
    msg = SimpleNamespace(usage=SimpleNamespace(input_tokens=120, output_tokens=45))
    assert usage.usage_from_anthropic(msg) == {"input_tokens": 120, "output_tokens": 45}
    assert usage.usage_from_anthropic(SimpleNamespace()) == {
        "input_tokens": None, "output_tokens": None}


def test_usage_from_openai_payload():
    data = {"usage": {"prompt_tokens": 900, "completion_tokens": 210}}
    assert usage.usage_from_openai_payload(data) == {
        "input_tokens": 900, "output_tokens": 210}
    assert usage.usage_from_openai_payload({}) == {
        "input_tokens": None, "output_tokens": None}
    assert usage.usage_from_openai_payload(None) == {
        "input_tokens": None, "output_tokens": None}


def test_usage_from_ollama_payload():
    data = {"prompt_eval_count": 512, "eval_count": 128, "done": True}
    assert usage.usage_from_ollama_payload(data) == {
        "input_tokens": 512, "output_tokens": 128}


def test_usage_from_voyage_payload():
    assert usage.usage_from_voyage_payload({"usage": {"total_tokens": 999}}) == {
        "total_tokens": 999}
    assert usage.usage_from_voyage_payload({}) == {"total_tokens": None}


# ── record() attribution ─────────────────────────────────────────────────────


async def test_record_with_context(pool, workspace_id):
    token = usage.set_context(workspace_id=workspace_id, surface="query",
                              document_id=77)
    try:
        await usage.record("llm", "anthropic", "claude-fable-5",
                           input_tokens=100, output_tokens=20)
    finally:
        usage.reset_context(token)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM usage_events")
    assert row["workspace_id"] == workspace_id
    assert row["surface"] == "query"
    assert row["document_id"] == 77
    assert row["total_tokens"] == 120  # derived from in+out


async def test_record_without_context_is_unknown(pool):
    await usage.record("embedding", "voyage", "voyage-3-lite", total_tokens=50)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM usage_events")
    assert row["workspace_id"] is None
    assert row["surface"] == "unknown"
    assert row["total_tokens"] == 50


async def test_formation_attributes_usage(pool, workspace_id, monkeypatch):
    """The worker sets the contextvar before formation; anything the provider
    layer records during the run must land attributed to the workspace/doc."""
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)

    result = make_formation_result()

    async def _fake(system, user, schema, **kw):
        # what the real anthropic branch does after its call
        await usage.record("llm", "anthropic", "claude-fable-5",
                           input_tokens=1000, output_tokens=200)
        return result

    monkeypatch.setattr(llm, "structured_call", _fake)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM usage_events WHERE kind='llm' ORDER BY id LIMIT 1")
    assert row["workspace_id"] == workspace_id
    assert row["surface"] == "formation"
    assert row["document_id"] == doc_id
    assert row["input_tokens"] == 1000


async def test_prune_respects_retention(pool, workspace_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO usage_events(workspace_id, surface, kind, provider, model, "
            "total_tokens, created_at) VALUES "
            "($1, 'query', 'llm', 'anthropic', 'm', 10, now() - interval '200 days'), "
            "($1, 'query', 'llm', 'anthropic', 'm', 10, now())",
            workspace_id,
        )
    await worker._prune_metrics()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM usage_events") == 1


# ── /api/ops/usage ───────────────────────────────────────────────────────────


async def _seed_usage(pool, workspace_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO usage_events(workspace_id, surface, kind, provider, model, "
            "input_tokens, output_tokens, total_tokens, request_count) VALUES "
            "($1, 'formation', 'llm', 'anthropic', 'claude-fable-5', 1000000, 500000, 1500000, 1), "
            "($1, 'formation', 'llm', 'anthropic', 'claude-fable-5', 1000000, 500000, 1500000, 1), "
            "($1, 'query', 'embedding', 'voyage', 'voyage-3-lite', NULL, NULL, 300, 1)",
            workspace_id,
        )


async def test_usage_endpoint_tokens_only(pool, workspace_id):
    await _seed_usage(pool, workspace_id)
    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.get("/api/ops/usage?days=7")
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 3
    assert body["input_tokens"] == 2_000_000
    assert body["output_tokens"] == 1_000_000
    assert body["total_tokens"] == 3_000_300
    assert body["cost_usd"] is None  # no COST_RATES_JSON configured
    surfaces = {(r["surface"], r["model"]) for r in body["breakdown"]}
    assert ("formation", "claude-fable-5") in surfaces
    assert ("query", "voyage-3-lite") in surfaces
    assert len(body["per_day"]) == 1


async def test_usage_endpoint_with_cost_rates(pool, workspace_id, monkeypatch):
    monkeypatch.setattr(
        config, "COST_RATES_JSON",
        '{"claude-fable-5": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}',
    )
    await _seed_usage(pool, workspace_id)
    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.get("/api/ops/usage?days=7")
    body = resp.json()
    # 2M input @ $3/M + 1M output @ $15/M = $21; voyage rows are unpriced
    assert body["cost_usd"] == 21.0
    priced = [r for r in body["breakdown"] if r["cost_usd"] is not None]
    assert len(priced) == 1 and priced[0]["cost_usd"] == 21.0
