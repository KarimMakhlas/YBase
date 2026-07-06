"""Review scratch test: exercise /api/ops/slo, /usage, /audit, /api/analytics/quality
and /api/health/details end-to-end with edge-case data to verify serialization,
workspace scoping, empty-table behavior, and the action filter."""

import json
import uuid

import pytest
import httpx

from app.core import db
from app.domains.auth import service as auth
from app.main import app


async def _seed(pool):
    async with pool.acquire() as conn:
        # two workspaces + users
        ws1 = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('ws1', 'rev-'||'') RETURNING id")
        ws2 = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES('ws2','rev-ws2') RETURNING id")
        u1 = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) "
            "VALUES('a@a.com','A','x') RETURNING id")
        # formation runs: ws1 has success w/ validation, failed w/ NULL duration,
        # timeout w/ empty validation; ws2 has one distinct run (leak canary)
        await conn.execute(
            "INSERT INTO formation_runs(workspace_id, status, duration_ms, queue_wait_ms, "
            "stage_timings, validation, started_at, finished_at) VALUES "
            "($1,'success',1200, 300, $3, $4, now() - interval '1 hour', now()),"
            "($1,'failed', NULL, NULL, '{}', '{}', now() - interval '2 hour', now()),"
            "($1,'timeout', 420000, 10, '{}', '{}', now() - interval '3 hour', now()),"
            "($2,'success', 99999, 1, $5, $6, now() - interval '1 hour', now())",
            ws1, ws2,
            {"fetch": 10, "llm": 1000, "persist": 100},
            {"invalid_cross_refs": 2, "empty_topics": 1,
             "trivial_reasoning": 0, "invalid_evidence_indexes": 0,
             "empty_extraction": False, "flagged": True,
             "details": ["decision 'x': unknown node refs [9]"]},
            {"fetch": 1, "llm": 88888, "persist": 1},
            {"invalid_cross_refs": 0, "empty_topics": 0,
             "trivial_reasoning": 0, "invalid_evidence_indexes": 0,
             "empty_extraction": False, "flagged": False},
        )
        # usage events: ws1 rows incl. NULL tokens (request-count only), ws2 canary
        await conn.execute(
            "INSERT INTO usage_events(workspace_id, surface, kind, provider, model, "
            "input_tokens, output_tokens, total_tokens, request_count) VALUES "
            "($1,'formation','llm','anthropic','claude-x', 1000, 200, 1200, 1),"
            "($1,'query','embedding','ollama','nomic', NULL, NULL, NULL, 3),"
            "($2,'formation','llm','anthropic','claude-x', 77777, 7, 77784, 1)",
            ws1, ws2)
        # audit events: differing actions, jsonb data; ws2 canary
        await conn.execute(
            "INSERT INTO audit_events(workspace_id, actor_user_id, action, target_type, "
            "target_id, data) VALUES "
            "($1,$3,'consolidation_merge_nodes','memory_node','5', $4),"
            "($1,$3,'formation_failed_permanently','document','9', '{}'),"
            "($2,$3,'consolidation_merge_nodes','memory_node','6', '{}')",
            ws1, ws2, u1, {"kept": 5, "merged": [6, 7]})
        return ws1, ws2, u1


def _ctx(u1, ws, role="admin"):
    return auth.AuthContext(
        user_id=u1, email="a@a.com", display_name="A", workspace_id=ws,
        workspace_name="ws", role=role, session_id=1, workspaces=[])


@pytest.mark.asyncio
async def test_ops_endpoints(pool):
    ws1, ws2, u1 = await _seed(pool)
    app.dependency_overrides[auth.get_current_user] = lambda: _ctx(u1, ws1)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as cx:
            # ---- /api/ops/slo ----
            r = await cx.get("/api/ops/slo?days=7")
            assert r.status_code == 200, r.text
            slo = r.json()
            print("SLO:", json.dumps(slo, indent=1)[:1500])
            assert slo["runs"] == 3          # ws2 run excluded
            assert slo["successes"] == 1
            assert slo["failures"] == 1
            assert slo["timeouts"] == 1
            # p95_llm must not include ws2's 88888ms llm stage
            assert slo["p95_llm_ms"] is not None and slo["p95_llm_ms"] < 80000

            # days clamping / validation
            r = await cx.get("/api/ops/slo?days=99999")
            assert r.status_code == 200 and r.json()["days"] == 90
            r = await cx.get("/api/ops/slo?days=-5")
            assert r.status_code == 200 and r.json()["days"] == 1
            r = await cx.get("/api/ops/slo?days=abc")
            assert r.status_code == 422
            r = await cx.get("/api/ops/slo?days=1%3B%20DROP%20TABLE%20x")
            assert r.status_code == 422

            # ---- /api/ops/usage ----
            r = await cx.get("/api/ops/usage?days=30")
            assert r.status_code == 200, r.text
            us = r.json()
            print("USAGE:", json.dumps(us, indent=1)[:1200])
            assert us["requests"] == 4               # 1 + 3, ws2 excluded
            assert us["total_tokens"] == 1200        # ws2's 77784 excluded
            assert us["cost_usd"] is None            # no COST_RATES_JSON
            models = {b["model"] for b in us["breakdown"]}
            assert models == {"claude-x", "nomic"}

            # with cost rates
            from app.core import config
            config.COST_RATES_JSON = '{"claude-x": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}'
            try:
                r = await cx.get("/api/ops/usage?days=30")
                assert r.status_code == 200, r.text
                us2 = r.json()
                assert us2["cost_usd"] == round(1000/1e6*3.0 + 200/1e6*15.0, 4)
                # malformed rates value -> ?
                config.COST_RATES_JSON = '{"claude-x": {"input_per_mtok": null}}'
                try:
                    r = await cx.get("/api/ops/usage?days=30")
                    print("malformed-rate status:", r.status_code, r.text[:300])
                except Exception as e:
                    print("malformed-rate CRASH:", type(e).__name__, e)
            finally:
                config.COST_RATES_JSON = ""

            # ---- /api/ops/audit ----
            r = await cx.get("/api/ops/audit?days=30")
            assert r.status_code == 200, r.text
            au = r.json()
            print("AUDIT:", json.dumps(au, indent=1)[:900])
            assert len(au["events"]) == 2            # ws2 event excluded
            assert all(isinstance(e["data"], dict) for e in au["events"])

            r = await cx.get("/api/ops/audit?days=30&actions=consolidation_merge_nodes")
            assert r.status_code == 200
            assert [e["action"] for e in r.json()["events"]] == ["consolidation_merge_nodes"]

            r = await cx.get("/api/ops/audit?days=30&actions=%20,%20")   # whitespace-only
            assert r.status_code == 200
            assert len(r.json()["events"]) == 2      # treated as no filter

            r = await cx.get("/api/ops/audit?days=30&actions=nope")
            assert r.status_code == 200 and r.json()["events"] == []

            # ---- /api/analytics/quality (validation aggregate) ----
            r = await cx.get("/api/analytics/quality")
            assert r.status_code == 200, r.text
            q = r.json()
            ev = [c for c in q["checks"] if c["key"] == "extraction_validation"][0]
            print("QUALITY extraction_validation:", ev)
            assert "1/1 runs flagged" in ev["detail"]   # only ws1 success run

            # ---- /api/health/details ----
            r = await cx.get("/api/health/details")
            assert r.status_code == 200, r.text
            print("HEALTH DETAILS redis:", r.json()["redis"])

            # ---- /api/health/formation (open endpoint) ----
            r = await cx.get("/api/health/formation")
            assert r.status_code == 200, r.text
            hf = r.json()
            print("HEALTH FORMATION:", json.dumps(hf))
            assert hf["completed_1h"] == 2  # instance-wide by design (ws1+ws2)

            # ---- role gating: viewer must be blocked ----
            app.dependency_overrides[auth.get_current_user] = (
                lambda: _ctx(u1, ws1, role="viewer"))
            for path in ("/api/ops/slo", "/api/ops/usage", "/api/ops/audit",
                         "/api/health/details"):
                r = await cx.get(path)
                assert r.status_code == 403, (path, r.status_code)

            # ---- empty tables (fresh workspace) ----
            app.dependency_overrides[auth.get_current_user] = lambda: _ctx(u1, ws2)
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM formation_runs WHERE workspace_id=$1", ws2)
                await conn.execute(
                    "DELETE FROM usage_events WHERE workspace_id=$1", ws2)
            r = await cx.get("/api/ops/slo")
            assert r.status_code == 200, r.text
            empty = r.json()
            assert empty["runs"] == 0 and empty["p50_ms"] is None
            r = await cx.get("/api/ops/usage")
            assert r.status_code == 200, r.text
            assert r.json()["requests"] == 0
            r = await cx.get("/api/analytics/quality")
            assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()
