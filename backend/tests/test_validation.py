"""Extraction validation: pure defect counting, and the wiring that lands the
report in formation_runs.validation and the quality endpoint."""

from app.domains.documents.ingestion import IngestRequest, ingest_document
from app.domains.memory import worker
from app.domains.memory.validation import validate_extraction

from conftest import make_formation_result
from test_api_endpoints import _auth_client


def _clean_result():
    return make_formation_result()


# ── Pure checks ──────────────────────────────────────────────────────────────


def test_clean_extraction_yields_zero_counts():
    report = validate_extraction(_clean_result(), valid_node_ids=set(),
                                 chunk_indexes={0})
    assert report["invalid_cross_refs"] == 0
    assert report["empty_topics"] == 0
    assert report["trivial_reasoning"] == 0
    assert report["invalid_evidence_indexes"] == 0
    assert report["empty_extraction"] is False
    assert report["flagged"] is False
    assert "details" not in report


def test_invalid_cross_refs_counted():
    result = make_formation_result(decisions=[{
        **_clean_result()["decisions"][0],
        "revisits_node_id": 999,          # unknown
        "relates_to_node_ids": [7, 1000],  # 7 valid, 1000 unknown
    }])
    report = validate_extraction(result, valid_node_ids={7}, chunk_indexes={0})
    assert report["invalid_cross_refs"] == 2
    assert report["flagged"] is True
    assert any("999" in d for d in report["details"])


def test_question_cross_refs_counted():
    result = make_formation_result(decisions=[], questions=[{
        "question": "Should we shard?", "status": "open", "resolution": None,
        "raised_by": [], "topics": [], "evidence_chunk_indexes": [0],
        "resolves_node_id": 555, "relates_to_node_ids": [],
    }])
    report = validate_extraction(result, valid_node_ids=set(), chunk_indexes={0})
    assert report["invalid_cross_refs"] == 1


def test_empty_topics_and_trivial_reasoning():
    base = _clean_result()["decisions"][0]
    result = make_formation_result(decisions=[
        {**base, "topics": ["", "  "]},          # effectively topicless
        {**base, "title": "Another decision", "reasoning": "short"},
    ])
    report = validate_extraction(result, valid_node_ids=set(), chunk_indexes={0})
    assert report["empty_topics"] == 1
    assert report["trivial_reasoning"] == 1


def test_reasoning_that_repeats_what_is_trivial():
    base = _clean_result()["decisions"][0]
    what = "We are adopting PostgreSQL for everything going forward, full stop."
    result = make_formation_result(decisions=[{**base, "what": what, "reasoning": what}])
    report = validate_extraction(result, valid_node_ids=set(), chunk_indexes={0})
    assert report["trivial_reasoning"] == 1


def test_invalid_evidence_indexes_counted():
    result = make_formation_result(decisions=[{
        **_clean_result()["decisions"][0],
        "evidence_chunk_indexes": [0, 5, 9],  # doc only has chunk 0
    }])
    report = validate_extraction(result, valid_node_ids=set(), chunk_indexes={0})
    assert report["invalid_evidence_indexes"] == 2


def test_empty_extraction_flagged():
    report = validate_extraction(
        {"context_summary": "", "decisions": [], "entities": [], "questions": []},
        valid_node_ids=set(), chunk_indexes={0})
    assert report["empty_extraction"] is True
    assert report["flagged"] is True


# ── DB wiring ────────────────────────────────────────────────────────────────


def _req(**over):
    base = dict(source="meeting", title="Validation test", text="Some content.")
    base.update(over)
    return IngestRequest(**base)


async def test_validation_lands_in_formation_runs(pool, workspace_id, fake_llm):
    fake_llm.result = make_formation_result(decisions=[{
        **_clean_result()["decisions"][0],
        "revisits_node_id": 424242,  # hallucinated ref
        "topics": [],
    }])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT validation FROM formation_runs WHERE document_id=$1", doc_id)
        # enforcement still happened: node exists with a fallback topic
        topics = await conn.fetchval(
            "SELECT count(*) FROM memory_edges e JOIN memory_nodes t ON t.id=e.dst "
            "WHERE e.workspace_id=$1 AND e.relation='about' AND t.kind='topic'",
            workspace_id)
    assert row["validation"]["invalid_cross_refs"] == 1
    assert row["validation"]["empty_topics"] == 1
    assert row["validation"]["flagged"] is True
    assert topics >= 1  # fallback_topics repaired it


async def test_quality_endpoint_surfaces_validation(pool, workspace_id, fake_llm):
    fake_llm.result = make_formation_result(decisions=[{
        **_clean_result()["decisions"][0],
        "revisits_node_id": 999999,
    }])
    doc_id, _ = await ingest_document(_req(), workspace_id=workspace_id)
    claimed = await worker._claim()
    await worker._run_one(claimed.doc_id)

    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.get("/api/analytics/quality")
    assert resp.status_code == 200
    checks = {c["key"]: c for c in resp.json()["checks"]}
    assert "extraction_validation" in checks
    assert checks["extraction_validation"]["status"] == "warn"  # 1/1 flagged
    assert "1 bad refs" in checks["extraction_validation"]["detail"] \
        or "1/1 runs flagged" in checks["extraction_validation"]["detail"]
