"""Per-document agent context (/api/agent/context-for-file): deterministic
path→term derivation and the briefing built from those terms."""

from app.domains.agent.service import derive_path_terms
from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import graph

from test_agent_api_db import _agent_client


# ── derive_path_terms is a pure function ─────────────────────────────────────

def test_derives_domain_terms_and_drops_noise():
    assert derive_path_terms("src/billing/charge.ts") == ["billing", "charge"]
    assert derive_path_terms("backend/app/domains/payments/refunds.py") == [
        "backend", "domains", "payments", "refunds"]


def test_splits_camelcase_dashes_and_underscores():
    assert derive_path_terms("src/components/BookingCancellationFlow.jsx") == [
        "booking", "cancellation", "flow"]
    assert derive_path_terms("lib/rate-limiter/token_bucket.go") == [
        "rate", "limiter", "token", "bucket"]


def test_extensions_short_words_and_digits_dropped():
    assert derive_path_terms("app/v2/db.py") == []
    assert derive_path_terms("config.json") == ["config"]
    assert derive_path_terms("tests/test_billing.py") == ["billing"]


def test_windows_separators_and_dedupe():
    assert derive_path_terms("src\\billing\\billing_rules.ts") == ["billing", "rules"]


def test_term_cap():
    path = "/".join(f"segment{c}" for c in "abcdefghij") + "/file.py"
    assert len(derive_path_terms(path)) <= 8


# ── endpoint behavior ─────────────────────────────────────────────────────────

async def test_file_context_returns_matching_decision(pool, workspace_id):
    async with pool.acquire() as conn:
        topic = await graph.upsert_node(conn, workspace_id, "topic", "billing")
        billing = await graph.upsert_node(
            conn, workspace_id, "decision", "Retry failed billing charges three times",
            summary="Charges retry up to three times with backoff.", status="decided")
        await graph.add_edge(conn, workspace_id, billing, topic, "about")
    # evidence chunk (real ingestion path) so retrieval has something to seed from
    doc_id, _ = await ingest_document(
        IngestRequest(source="meeting", title="Billing policy",
                      text="We retry failed billing charges three times."),
        workspace_id=workspace_id,
    )
    async with pool.acquire() as conn:
        chunk = await conn.fetchval(
            "SELECT id FROM chunks WHERE document_id=$1 ORDER BY chunk_index LIMIT 1",
            doc_id)
        await graph.link_chunk(conn, chunk, billing)

    async with await _agent_client(pool, workspace_id) as agent:
        hit = (await agent.post("/api/agent/context-for-file",
                                json={"path": "src/billing/charge.ts"})).json()
        miss = (await agent.post("/api/agent/context-for-file",
                                 json={"path": "docs/hiring/onboarding.md"})).json()
        empty = (await agent.post("/api/agent/context-for-file",
                                  json={"path": "app/v2/db.py"})).json()
        bad = await agent.post("/api/agent/context-for-file", json={"path": "  "})

    assert hit["derived_terms"] == ["billing", "charge"]
    assert any(d["node_id"] == billing for d in hit["relevant_decisions"])

    # Unrelated path: terms derive correctly. (No exclusion assertion — vector
    # search is top-K without a similarity floor, so with a single document in
    # the workspace its chunks surface for any query; exclusion is a ranking
    # property, not a guarantee.)
    assert miss["derived_terms"] == ["docs", "hiring", "onboarding"]

    assert empty["derived_terms"] == []
    assert empty["relevant_decisions"] == []
    assert bad.status_code == 400
