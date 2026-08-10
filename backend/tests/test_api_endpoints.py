"""HTTP-level coverage for auth, protected routes, and query SSE."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from app.core import config, migrate
from app.domains.auth import service as auth
from app.main import app


def _transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=app)


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport(), base_url="http://testserver")


async def _auth_client(pool, workspace_id: int, role: str = "admin"):
    email = f"{role}-{uuid4().hex}@example.test"
    token = f"test-{uuid4().hex}"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) "
            "VALUES($1, $2, $3) RETURNING id",
            email,
            f"{role.title()} User",
            await auth.hash_password("correct horse battery staple"),
        )
        await conn.execute(
            "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
            "VALUES($1, $2, $3)",
            workspace_id,
            user_id,
            role,
        )
        await conn.execute(
            "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, expires_at) "
            "VALUES($1, $2, $3, $4)",
            user_id,
            workspace_id,
            auth._hash_token(token),
            datetime.now(timezone.utc) + timedelta(days=1),
        )
    client = await _client()
    client.cookies.set(auth.COOKIE_NAME, token, path="/")
    return client, user_id


async def test_bootstrap_login_logout_cookie_flow(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE answer_feedback, auth_sessions, workspace_memberships, users, "
            "workspaces, audit_events, auth_login_attempts RESTART IDENTITY CASCADE"
        )
    await migrate.run()

    async with await _client() as client:
        status = await client.get("/api/auth/bootstrap-status")
        assert status.status_code == 200
        assert status.json()["needs_bootstrap"] is True

        boot = await client.post(
            "/api/auth/bootstrap",
            json={
                "workspace_name": "Test Workspace",
                "email": "owner@example.test",
                "display_name": "Owner",
                "password": "correct horse battery staple",
            },
        )
        assert boot.status_code == 200
        assert auth.COOKIE_NAME in client.cookies
        assert boot.json()["workspace"]["role"] == "owner"

        second = await client.post(
            "/api/auth/bootstrap",
            json={
                "workspace_name": "Other",
                "email": "second@example.test",
                "display_name": "Second",
                "password": "correct horse battery staple",
            },
        )
        assert second.status_code == 409

        logout = await client.post("/api/auth/logout")
        assert logout.status_code == 200

        login = await client.post(
            "/api/auth/login",
            json={
                "email": "owner@example.test",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200

        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["email"] == "owner@example.test"


async def test_protected_ingest_requires_admin(pool, workspace_id):
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        denied = await member.post(
            "/api/ingest",
            json={"source": "meeting", "title": "Notes", "text": "Member cannot ingest."},
        )
        assert denied.status_code == 403

    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        created = await admin.post(
            "/api/ingest",
            json={
                "source": "meeting",
                "title": "Architecture notes",
                "text": "We decided to keep PostgreSQL for the first release.",
                "tags": ["architecture"],
            },
        )
        assert created.status_code == 200
        assert created.json()["duplicate"] is False
        doc_id = created.json()["document_id"]

        docs = await admin.get("/api/documents")
        assert docs.status_code == 200
        assert any(d["id"] == doc_id for d in docs.json())


async def test_document_detail_exposes_active_formation_lineage(
    pool, workspace_id, fake_llm
):
    from app.domains.memory.formation import run_formation

    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        ingest = await admin.post(
            "/api/ingest",
            json={
                "source": "meeting",
                "title": "Lineage notes",
                "text": "We chose PostgreSQL because transactions matter.",
            },
        )
        doc_id = ingest.json()["document_id"]
        await run_formation(doc_id)
        detail = await admin.get(f"/api/documents/{doc_id}")

    assert detail.status_code == 200
    formation = detail.json()["formation"]
    assert formation["active_run_id"]
    assert formation["quarantined_observations"] == 0


async def test_query_streams_sse_events(pool, workspace_id, monkeypatch):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        await admin.post(
            "/api/ingest",
            json={
                "source": "meeting",
                "title": "Decision notes",
                "text": "The team decided to keep PostgreSQL for v1.",
            },
        )

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        @property
        def text_stream(self):
            async def gen():
                yield "PostgreSQL stayed the default [C1]."
                yield "\n<<<MEMORY_METADATA>>>\n"
                yield (
                    '{"confidence":"high","cited_chunk_ids":[1],'
                    '"related_questions":[],"timeline":[]}'
                )

            return gen()

    monkeypatch.setattr("app.providers.llm.stream_text", lambda *_args, **_kwargs: FakeStream())

    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        resp = await member.post("/api/query", json={"question": "What database did we pick?"})
        assert resp.status_code == 200
        body = resp.text
        assert "event: status" in body
        assert "event: delta" in body
        assert "event: metadata" in body
        assert "event: done" in body


async def test_query_strips_metadata_bleed_from_answer(pool, workspace_id, monkeypatch):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        ingest = await admin.post(
            "/api/ingest",
            json={
                "source": "meeting",
                "title": "Decision notes",
                "text": "The team decided to keep PostgreSQL for v1.",
            },
        )
    doc_id = ingest.json()["document_id"]
    async with pool.acquire() as conn:
        chunk_id = await conn.fetchval(
            "SELECT id FROM chunks WHERE document_id=$1 ORDER BY chunk_index LIMIT 1",
            doc_id,
        )

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        @property
        def text_stream(self):
            async def gen():
                yield f"PostgreSQL stayed the default [C{chunk_id}]."
                yield "\n\n| Caveat | Supporting Chunk(s) |\n"
                yield "|---|---|\n| MongoDB was faster for writes | [C4] |\n"
                yield "\n<<<MEMORY_METADATA>>>\n"
                yield json.dumps({
                    "takeaway": "PostgreSQL stayed the default.",
                    "confidence": "high",
                    "cited_chunk_ids": [chunk_id],
                    "related_questions": [],
                    "timeline": [],
                    "counter_evidence": [
                        {"point": "MongoDB was faster for writes", "chunk_ids": [chunk_id]},
                    ],
                    "insight_cards": [
                        {
                            "type": "why_it_won",
                            "title": "Why Postgres won",
                            "items": [
                                {
                                    "label": "Low operational risk",
                                    "detail": "The team could keep one datastore for v1.",
                                    "chunk_ids": [chunk_id],
                                },
                            ],
                        },
                    ],
                })

            return gen()

    monkeypatch.setattr("app.providers.llm.stream_text", lambda *_args, **_kwargs: FakeStream())

    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        resp = await member.post("/api/query", json={"question": "What database did we pick?"})
        assert resp.status_code == 200
        body = resp.text
        assert "PostgreSQL stayed the default" in body
        assert "Caveat" not in body
        assert "MongoDB was faster for writes" in body  # still present in metadata card data
        assert '"takeaway": "PostgreSQL stayed the default."' in body
        assert '"insight_cards": [{"type": "why_it_won"' in body
        assert f'"sources": [{{"chunk_id": {chunk_id}' in body


async def test_query_rate_limit_returns_429(pool, workspace_id, monkeypatch):
    from app.api.routes import query as query_route

    async def fake_stream(*_args, **_kwargs):
        yield 'event: done\ndata: {}\n\n'

    monkeypatch.setattr(query_route, "stream_query", fake_stream)
    old_max = query_route.query_limiter.max_events
    query_route.query_limiter.max_events = 1
    query_route.query_limiter._events.clear()
    try:
        member, _ = await _auth_client(pool, workspace_id, role="member")
        async with member:
            first = await member.post("/api/query", json={"question": "one"})
            second = await member.post("/api/query", json={"question": "two"})
        assert first.status_code == 200
        assert second.status_code == 429
    finally:
        query_route.query_limiter.max_events = old_max
        query_route.query_limiter._events.clear()


async def _create_saved_answer(client, title="Feedback target"):
    sess = await client.post("/api/sessions", json={"title": title})
    assert sess.status_code == 200
    sid = sess.json()["id"]
    user_msg = await client.post(
        f"/api/sessions/{sid}/messages",
        json={"role": "user", "content": "What database did we choose?"},
    )
    assert user_msg.status_code == 200
    assistant_msg = await client.post(
        f"/api/sessions/{sid}/messages",
        json={
            "role": "assistant",
            "content": "We chose Postgres. [C1]",
            "meta": {
                "confidence": "high",
                "citations": [
                    {
                        "chunk_id": 1,
                        "document_id": 1,
                        "title": "Architecture notes",
                        "source": "meeting",
                        "snippet": "Postgres was selected.",
                    }
                ],
                "trace": {"nodes": [{"id": 42, "kind": "decision", "label": "Use Postgres"}]},
            },
        },
    )
    assert assistant_msg.status_code == 200
    return sid, assistant_msg.json()["id"]


async def test_member_can_submit_feedback_for_own_answer(pool, workspace_id):
    member, user_id = await _auth_client(pool, workspace_id, role="member")
    async with member:
        _sid, message_id = await _create_saved_answer(member)
        feedback = await member.post(
            "/api/answer-feedback",
            json={
                "chat_message_id": message_id,
                "issue_type": "wrong",
                "note": "This ignores the later reversal.",
            },
        )
        assert feedback.status_code == 200
        body = feedback.json()
        assert body["reporter_user_id"] == user_id
        assert body["issue_type"] == "wrong"
        assert body["status"] == "open"

        mine = await member.get(f"/api/answer-feedback/mine?chat_message_id={message_id}")
        assert mine.status_code == 200
        assert mine.json()["id"] == body["id"]


async def test_member_cannot_submit_feedback_for_another_users_message(pool, workspace_id):
    owner_client, _owner_id = await _auth_client(pool, workspace_id, role="member")
    async with owner_client:
        _sid, message_id = await _create_saved_answer(owner_client)

    other_client, _other_id = await _auth_client(pool, workspace_id, role="member")
    async with other_client:
        denied = await other_client.post(
            "/api/answer-feedback",
            json={"chat_message_id": message_id, "issue_type": "wrong"},
        )
        assert denied.status_code == 403


async def test_feedback_admin_queue_permissions_and_resolution(pool, workspace_id):
    member, _member_id = await _auth_client(pool, workspace_id, role="member")
    async with member:
        _sid, message_id = await _create_saved_answer(member)
        created = await member.post(
            "/api/answer-feedback",
            json={"chat_message_id": message_id, "issue_type": "bad_citation"},
        )
        feedback_id = created.json()["id"]
        denied = await member.get("/api/answer-feedback")
        assert denied.status_code == 403

    admin, _admin_id = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        queue = await admin.get("/api/answer-feedback?status=open")
        assert queue.status_code == 200
        assert any(f["id"] == feedback_id for f in queue.json())

        detail = await admin.get(f"/api/answer-feedback/{feedback_id}")
        assert detail.status_code == 200
        assert detail.json()["citations"][0]["title"] == "Architecture notes"

        resolved = await admin.patch(
            f"/api/answer-feedback/{feedback_id}",
            json={"status": "resolved", "resolution_note": "Fixed in memory review."},
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["resolution_note"] == "Fixed in memory review."


async def test_duplicate_feedback_updates_existing_row(pool, workspace_id):
    member, _member_id = await _auth_client(pool, workspace_id, role="member")
    async with member:
        _sid, message_id = await _create_saved_answer(member)
        first = await member.post(
            "/api/answer-feedback",
            json={"chat_message_id": message_id, "issue_type": "wrong", "note": "first"},
        )
        second = await member.post(
            "/api/answer-feedback",
            json={"chat_message_id": message_id, "issue_type": "outdated", "note": "second"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["issue_type"] == "outdated"
        assert second.json()["note"] == "second"

    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM answer_feedback WHERE chat_message_id=$1",
            message_id,
        )
    assert count == 1


async def test_feedback_is_workspace_scoped(pool, workspace_id):
    other_workspace = await _make_workspace(pool, "Feedback Other")
    member_a, _ = await _auth_client(pool, workspace_id, role="member")
    async with member_a:
        _sid, message_id = await _create_saved_answer(member_a)
        created = await member_a.post(
            "/api/answer-feedback",
            json={"chat_message_id": message_id, "issue_type": "wrong"},
        )
        assert created.status_code == 200

    admin_b, _ = await _auth_client(pool, other_workspace, role="admin")
    async with admin_b:
        queue = await admin_b.get("/api/answer-feedback?status=open")
        assert queue.status_code == 200
        assert queue.json() == []
        detail = await admin_b.get(f"/api/answer-feedback/{created.json()['id']}")
        assert detail.status_code == 404


async def test_ops_overview_is_admin_only_and_reports_readiness(pool, workspace_id):
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        denied = await member.get("/api/ops/overview")
        assert denied.status_code == 403

    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        overview = await admin.get("/api/ops/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["workspace"]["id"] == workspace_id
        assert "readiness" in body
        assert any(s["key"] == "add_memory" for s in body["readiness"]["steps"])


async def test_ops_demo_seed_ingests_demo_documents(pool, workspace_id):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        seeded = await admin.post("/api/ops/demo-seed")
        assert seeded.status_code == 200
        body = seeded.json()
        assert body["created"] == 4
        assert len(body["document_ids"]) == 4
        again = await admin.post("/api/ops/demo-seed")
        assert again.status_code == 200
        assert again.json()["duplicates"] == 4


async def test_ops_retry_failed_documents(pool, workspace_id):
    async with pool.acquire() as conn:
        doc_id = await conn.fetchval(
            "INSERT INTO documents(workspace_id, source, title, raw_text, formation_status, "
            "formation_error, formation_attempts) VALUES($1, 'meeting', 'Broken notes', "
            "'bad json', 'failed', 'model failed', 3) RETURNING id",
            workspace_id,
        )
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        retry = await admin.post("/api/ops/failed-documents/retry")
        assert retry.status_code == 200
        assert retry.json()["document_ids"] == [doc_id]

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT formation_status, formation_error, formation_attempts "
            "FROM documents WHERE id=$1",
            doc_id,
        )
    assert row["formation_status"] == "pending"
    assert row["formation_error"] is None
    assert row["formation_attempts"] == 0


async def test_retry_sync_job_requeues_failed_job(pool, workspace_id, monkeypatch):
    from app.domains.connectors import service as sources

    async def fake_run_slack_sync_job(_job_id):
        return None

    monkeypatch.setattr(sources, "run_slack_sync_job", fake_run_slack_sync_job)
    async with pool.acquire() as conn:
        connection_id = await conn.fetchval(
            "INSERT INTO source_connections(workspace_id, provider, name, status, external_workspace_id) "
            "VALUES($1, 'slack', 'Demo Slack', 'connected', $2) RETURNING id",
            workspace_id, f"T-{uuid4().hex[:8]}",
        )
        stream_id = await conn.fetchval(
            "INSERT INTO source_streams(workspace_id, connection_id, provider, external_id, name, "
            "selected, status, last_error) VALUES($1, $2, 'slack', 'C1', 'engineering', "
            "true, 'failed', 'boom') RETURNING id",
            workspace_id, connection_id,
        )
        job_id = await conn.fetchval(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, error) "
            "VALUES($1, $2, 'slack', 'failed', 'backfill', 'boom') RETURNING id",
            workspace_id, connection_id,
        )

    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        retry = await admin.post(f"/api/sources/{connection_id}/jobs/{job_id}/retry")
        assert retry.status_code == 200
        assert retry.json()["status"] == "pending"

    async with pool.acquire() as conn:
        job_status = await conn.fetchval("SELECT status FROM sync_jobs WHERE id=$1", job_id)
        stream = await conn.fetchrow("SELECT status, last_error FROM source_streams WHERE id=$1", stream_id)
    assert job_status == "pending"
    assert stream["status"] == "idle"
    assert stream["last_error"] is None


async def _make_workspace(pool, name: str) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
            name,
            f"{name.lower().replace(' ', '-')}-{uuid4().hex[:8]}",
        )


# --- Chunk 1: auth hardening -------------------------------------------------

async def test_register_is_rate_limited(pool):
    """A burst of signups from one client is throttled to a 429."""
    auth.auth_limiter.max_events = 1
    auth.auth_limiter._events.clear()
    try:
        async with await _client() as client:
            body = lambda: {
                "email": f"signup-{uuid4().hex}@example.test",
                "display_name": "Sign Up",
                "password": "correct horse battery staple",
            }
            first = await client.post("/api/auth/register", json=body())
            second = await client.post("/api/auth/register", json=body())
        assert first.status_code == 200
        assert second.status_code == 429
    finally:
        auth.auth_limiter.max_events = 1000
        auth.auth_limiter._events.clear()


async def test_password_change_requires_correct_current(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="member")
    async with client:
        wrong = await client.patch(
            "/api/auth/me",
            json={"current_password": "nope", "new_password": "a brand new password"},
        )
        assert wrong.status_code == 403
        ok = await client.patch(
            "/api/auth/me",
            json={
                "current_password": "correct horse battery staple",
                "new_password": "a brand new password",
            },
        )
        assert ok.status_code == 200
        assert "password" in ok.json()["changed"]


async def test_password_change_revokes_other_sessions(pool, workspace_id):
    client, user_id = await _auth_client(pool, workspace_id, role="member")
    other_token = f"other-{uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, expires_at) "
            "VALUES($1, $2, $3, $4)",
            user_id, workspace_id, auth._hash_token(other_token),
            datetime.now(timezone.utc) + timedelta(days=1),
        )
    other = await _client()
    other.cookies.set(auth.COOKIE_NAME, other_token, path="/")
    async with client, other:
        assert (await other.get("/api/auth/me")).status_code == 200
        changed = await client.patch(
            "/api/auth/me",
            json={
                "current_password": "correct horse battery staple",
                "new_password": "a brand new password",
            },
        )
        assert changed.status_code == 200
        # current session survives, the other one is revoked
        assert (await client.get("/api/auth/me")).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 401


async def test_forgot_password_is_quiet_and_issues_token(pool, workspace_id):
    email = f"forgot-{uuid4().hex}@example.test"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) VALUES($1, $2, $3) "
            "RETURNING id",
            email, "Forgot User", await auth.hash_password("correct horse battery staple"),
        )
    async with await _client() as client:
        known = await client.post("/api/auth/forgot", json={"email": email})
        unknown = await client.post(
            "/api/auth/forgot", json={"email": f"nobody-{uuid4().hex}@example.test"}
        )
    assert known.status_code == 200 and known.json() == {"ok": True}
    assert unknown.status_code == 200 and unknown.json() == {"ok": True}
    async with pool.acquire() as conn:
        issued = await conn.fetchval(
            "SELECT count(*) FROM password_reset_tokens WHERE user_id=$1", user_id
        )
    assert issued == 1  # only the real account got a token


async def test_register_starts_unverified_and_issues_a_verification_token(pool):
    email = f"newsignup-{uuid4().hex}@example.test"
    async with await _client() as client:
        resp = await client.post("/api/auth/register", json={
            "email": email, "display_name": "New Signup",
            "password": "correct horse battery staple",
        })
    assert resp.status_code == 200
    assert resp.json()["user"]["email_verified"] is False
    async with pool.acquire() as conn:
        issued = await conn.fetchval(
            "SELECT count(*) FROM email_verification_tokens t JOIN users u ON u.id=t.user_id "
            "WHERE lower(u.email)=lower($1)",
            email,
        )
    assert issued == 1


async def test_verify_email_consumes_token_and_is_idempotent(pool):
    email = f"verify-{uuid4().hex}@example.test"
    raw = f"vtok-{uuid4().hex}"
    user_id = await _password_user(pool, email, verified=False)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_verification_tokens(user_id, token_hash, expires_at) "
            "VALUES($1, $2, now() + interval '1 hour')",
            user_id, auth._hash_token(raw),
        )
    async with await _client() as client:
        first = await client.post("/api/auth/verify", json={"token": raw})
        # Mail clients and link scanners fetch links more than once.
        second = await client.post("/api/auth/verify", json={"token": raw})
    assert first.status_code == 200 and first.json()["already_verified"] is False
    assert second.status_code == 200 and second.json()["already_verified"] is True
    async with pool.acquire() as conn:
        verified_at = await conn.fetchval(
            "SELECT email_verified_at FROM users WHERE id=$1", user_id
        )
    assert verified_at is not None


async def test_verify_email_rejects_expired_and_unknown_tokens(pool):
    email = f"vexpired-{uuid4().hex}@example.test"
    raw = f"vtok-{uuid4().hex}"
    user_id = await _password_user(pool, email, verified=False)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_verification_tokens(user_id, token_hash, expires_at) "
            "VALUES($1, $2, now() - interval '1 minute')",
            user_id, auth._hash_token(raw),
        )
    async with await _client() as client:
        expired = await client.post("/api/auth/verify", json={"token": raw})
        unknown = await client.post("/api/auth/verify", json={"token": "no-such-token"})
    assert expired.status_code == 400
    assert unknown.status_code == 400
    async with pool.acquire() as conn:
        still_null = await conn.fetchval(
            "SELECT email_verified_at FROM users WHERE id=$1", user_id
        )
    assert still_null is None


async def test_verified_account_accepts_the_google_link_after_verifying(pool, monkeypatch):
    """The other half of the takeover fix: once the address IS verified, the
    normal auto-link path works again — the guard blocks squatters, not users."""
    email = f"gverified-{uuid4().hex}@example.test"
    raw = f"vtok-{uuid4().hex}"
    user_id = await _password_user(pool, email, verified=False)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO email_verification_tokens(user_id, token_hash, expires_at) "
            "VALUES($1, $2, now() + interval '1 hour')",
            user_id, auth._hash_token(raw),
        )
    state = await _seed_login_state(pool)
    sub = f"sub-{uuid4().hex}"
    monkeypatch.setattr(
        "app.domains.auth.service._google_fetch_identity",
        _fake_identity(sub=sub, email=email, name="Owner"),
    )
    async with await _client() as client:
        assert (await client.post("/api/auth/verify", json={"token": raw})).status_code == 200
        resp = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert resp.status_code in (302, 307)
        assert "unverified_account" not in resp.headers["location"]
    async with pool.acquire() as conn:
        linked = await conn.fetchval("SELECT google_sub FROM users WHERE id=$1", user_id)
    assert linked == sub


async def test_reset_password_consumes_token_and_revokes_sessions(pool, workspace_id):
    email = f"reset-{uuid4().hex}@example.test"
    raw = f"reset-{uuid4().hex}"
    sess = f"sess-{uuid4().hex}"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) VALUES($1, $2, $3) "
            "RETURNING id",
            email, "Reset User", await auth.hash_password("the old password here"),
        )
        await conn.execute(
            "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
            "VALUES($1, $2, 'member')",
            workspace_id, user_id,
        )
        await conn.execute(
            "INSERT INTO password_reset_tokens(user_id, token_hash, expires_at) "
            "VALUES($1, $2, now() + interval '1 hour')",
            user_id, auth._hash_token(raw),
        )
        await conn.execute(
            "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, expires_at) "
            "VALUES($1, $2, $3, now() + interval '1 day')",
            user_id, workspace_id, auth._hash_token(sess),
        )
    async with await _client() as client:
        done = await client.post(
            "/api/auth/reset", json={"token": raw, "new_password": "a fresh new password"}
        )
        assert done.status_code == 200
        # token is single-use
        replay = await client.post(
            "/api/auth/reset", json={"token": raw, "new_password": "yet another password"}
        )
        assert replay.status_code == 400
        # new password works, old one no longer does
        good = await client.post(
            "/api/auth/login", json={"email": email, "password": "a fresh new password"}
        )
        assert good.status_code == 200
    async with pool.acquire() as conn:
        revoked = await conn.fetchval(
            "SELECT revoked_at FROM auth_sessions WHERE token_hash=$1",
            auth._hash_token(sess),
        )
    assert revoked is not None  # the pre-existing session was killed by the reset


async def test_reset_password_rejects_expired_token(pool, workspace_id):
    email = f"expired-{uuid4().hex}@example.test"
    raw = f"expired-{uuid4().hex}"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) VALUES($1, $2, $3) "
            "RETURNING id",
            email, "Expired User", await auth.hash_password("correct horse battery staple"),
        )
        await conn.execute(
            "INSERT INTO password_reset_tokens(user_id, token_hash, expires_at) "
            "VALUES($1, $2, now() - interval '1 minute')",
            user_id, auth._hash_token(raw),
        )
    async with await _client() as client:
        resp = await client.post(
            "/api/auth/reset", json={"token": raw, "new_password": "a fresh new password"}
        )
    assert resp.status_code == 400


# --- Chunk 2: onboarding + single-owner roles --------------------------------

async def test_register_creates_no_workspace(pool):
    """Identity-only signup: the account exists and authenticates, but has no
    workspace until the wizard creates one."""
    async with await _client() as client:
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": f"solo-{uuid4().hex}@example.test",
                "display_name": "Solo Founder",
                "password": "correct horse battery staple",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspace"] is None
        assert body["workspaces"] == []
        # The workspace-less session still authenticates.
        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["workspace"] is None


async def test_workspace_create_makes_owner_and_points_session(pool):
    """Wizard step 1: create a workspace → current user becomes its sole owner
    and the active session is pointed at it."""
    async with await _client() as client:
        await client.post(
            "/api/auth/register",
            json={
                "email": f"founder-{uuid4().hex}@example.test",
                "display_name": "Founder",
                "password": "correct horse battery staple",
            },
        )
        created = await client.post("/api/workspace/create", json={"name": "Acme Eng"})
        assert created.status_code == 200
        ws = created.json()["workspace"]
        assert ws["name"] == "Acme Eng"
        assert ws["role"] == "owner"
        # Subsequent calls now operate inside the new workspace.
        me = await client.get("/api/auth/me")
        assert me.json()["workspace"]["name"] == "Acme Eng"


async def test_create_workspace_user_cannot_mint_owner(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="owner")
    async with client:
        resp = await client.post(
            "/api/workspace/users",
            json={
                "email": f"new-{uuid4().hex}@example.test",
                "display_name": "New Owner",
                "password": "correct horse battery staple",
                "role": "owner",
            },
        )
        assert resp.status_code == 400


async def test_transfer_ownership_swaps_roles(pool):
    ws = await _make_workspace(pool, "Transfer Co")
    owner_client, owner_id = await _auth_client(pool, ws, role="owner")
    _, member_id = await _auth_client(pool, ws, role="member")
    async with owner_client:
        resp = await owner_client.post(
            "/api/workspace/transfer-ownership",
            json={"new_owner_user_id": member_id},
        )
        assert resp.status_code == 200
    async with pool.acquire() as conn:
        rows = dict(
            (r["user_id"], r["role"])
            for r in await conn.fetch(
                "SELECT user_id, role FROM workspace_memberships WHERE workspace_id=$1",
                ws,
            )
        )
    assert rows[member_id] == "owner"
    assert rows[owner_id] == "admin"


async def test_transfer_ownership_requires_owner(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="admin")
    async with client:
        resp = await client.post(
            "/api/workspace/transfer-ownership", json={"new_owner_user_id": 999999}
        )
        assert resp.status_code == 403


async def test_onboarding_status_reports_steps(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="owner")
    async with client:
        resp = await client.get("/api/workspace/onboarding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["steps"]["invited"] is False
        assert body["steps"]["context"] is False
        assert body["steps"]["asked"] is False
        assert body["complete"] is False
        assert body["onboarded_at"] is None
        done = await client.post("/api/workspace/onboarding/complete")
        assert done.status_code == 200
        after = await client.get("/api/workspace/onboarding")
        assert after.json()["onboarded_at"] is not None


# ---- Google sign-in ----

def _fake_identity(sub, email, name, email_verified=True):
    """Build a stand-in for auth._google_fetch_identity (the network round-trip
    to Google's token + userinfo endpoints)."""
    async def fetch(_code):
        return {
            "sub": sub,
            "email": email,
            "name": name,
            "email_verified": email_verified,
        }
    return fetch


async def _seed_login_state(pool) -> str:
    state = f"state-{uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO oauth_login_states(state, provider, redirect_path, expires_at) "
            "VALUES($1, 'google', 'http://localhost:5173', now() + interval '10 minutes')",
            state,
        )
    return state


async def test_auth_providers_reports_google_disabled(pool):
    """Google is unconfigured in tests → the probe says so and /google/start 404s."""
    async with await _client() as client:
        providers = await client.get("/api/auth/providers")
        assert providers.status_code == 200
        assert providers.json()["google"] is False
        start = await client.get("/api/auth/google/start")
        assert start.status_code == 404


async def test_google_callback_creates_passwordless_user(pool, monkeypatch):
    """A first-time Google user gets a fresh passwordless account, a session
    cookie, and no workspace yet (→ Chunk-2 wizard)."""
    state = await _seed_login_state(pool)
    email = f"gnew-{uuid4().hex}@example.test"
    monkeypatch.setattr(
        "app.domains.auth.service._google_fetch_identity",
        _fake_identity(sub=f"sub-{uuid4().hex}", email=email, name="Gina New"),
    )
    async with await _client() as client:
        resp = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert resp.status_code in (302, 307)
        assert auth.COOKIE_NAME in resp.cookies
        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        body = me.json()
        assert body["user"]["email"] == email
        assert body["workspace"] is None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash, auth_provider, google_sub "
            "FROM users WHERE lower(email)=lower($1)",
            email,
        )
    assert row["password_hash"] is None
    assert row["auth_provider"] == "google"
    assert row["google_sub"]


async def _password_user(pool, email: str, verified: bool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash, email_verified_at) "
            "VALUES($1, $2, $3, CASE WHEN $4 THEN now() END) RETURNING id",
            email, "Pat Password",
            await auth.hash_password("correct horse battery staple"), verified,
        )


async def test_google_callback_links_existing_verified_password_account(pool, monkeypatch):
    """Signing in with Google on an email that already has a VERIFIED password
    account links to that same user (no duplicate) and stamps google_sub.
    Matching is case-insensitive on email."""
    email = f"glink-{uuid4().hex}@example.test"
    existing_id = await _password_user(pool, email, verified=True)
    state = await _seed_login_state(pool)
    sub = f"sub-{uuid4().hex}"
    monkeypatch.setattr(
        "app.domains.auth.service._google_fetch_identity",
        _fake_identity(sub=sub, email=email.upper(), name="Pat"),
    )
    async with await _client() as client:
        resp = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert resp.status_code in (302, 307)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, google_sub FROM users WHERE lower(email)=lower($1)", email
        )
    assert len(rows) == 1
    assert rows[0]["id"] == existing_id
    assert rows[0]["google_sub"] == sub


async def test_google_callback_refuses_unverified_password_account(pool, monkeypatch):
    """Account-takeover regression. An attacker registers victim@corp.com with
    their own password and never verifies it. When the real owner signs in with
    Google, auto-linking that identity onto the squatted row would hand the
    attacker a password on the victim's account — so it must be refused, leaving
    google_sub unset and no session cookie issued."""
    email = f"gsquat-{uuid4().hex}@example.test"
    squatted_id = await _password_user(pool, email, verified=False)
    state = await _seed_login_state(pool)
    sub = f"sub-{uuid4().hex}"
    monkeypatch.setattr(
        "app.domains.auth.service._google_fetch_identity",
        _fake_identity(sub=sub, email=email, name="Real Owner"),
    )
    async with await _client() as client:
        resp = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert resp.status_code in (302, 307)
        assert "unverified_account" in resp.headers["location"]
        assert auth.COOKIE_NAME not in resp.cookies
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, google_sub FROM users WHERE lower(email)=lower($1)", email
        )
    # The squatted row is untouched and no shadow account was created.
    assert len(rows) == 1
    assert rows[0]["id"] == squatted_id
    assert rows[0]["google_sub"] is None


async def test_google_callback_rejects_unknown_state(pool):
    """An invalid/expired state is refused before any token exchange — no cookie,
    redirected back to login with an error flag."""
    async with await _client() as client:
        resp = await client.get("/api/auth/google/callback?code=abc&state=nope")
        assert resp.status_code in (302, 307)
        assert "auth_error" in resp.headers["location"]
        assert auth.COOKIE_NAME not in resp.cookies


async def test_google_callback_state_is_single_use(pool, monkeypatch):
    """The state row is consumed on first use, so a replayed callback fails."""
    state = await _seed_login_state(pool)
    monkeypatch.setattr(
        "app.domains.auth.service._google_fetch_identity",
        _fake_identity(sub=f"sub-{uuid4().hex}", email=f"greplay-{uuid4().hex}@example.test", name="Re Play"),
    )
    async with await _client() as client:
        first = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert first.status_code in (302, 307)
        assert auth.COOKIE_NAME in first.cookies
        second = await client.get(f"/api/auth/google/callback?code=abc&state={state}")
        assert "auth_error" in second.headers["location"]


async def test_login_on_google_only_account_points_to_google(pool):
    """Password login on a Google-only account returns a friendly 'use Google'
    message rather than a generic invalid-password error."""
    email = f"gonly-{uuid4().hex}@example.test"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users(email, display_name, password_hash, auth_provider, google_sub) "
            "VALUES($1, $2, NULL, 'google', $3)",
            email, "Goo Only", f"sub-{uuid4().hex}",
        )
    async with await _client() as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": email, "password": "irrelevant but long enough"},
        )
        assert resp.status_code == 401
        assert "Google" in resp.json()["detail"]


# ---- Billing: trial data model + read-only gating (Stripe stubbed) ----

async def _workspace_with_billing(pool, plan_status="trialing", trial_ends_at=None) -> int:
    """A throwaway workspace with explicit billing state, so a test can force
    'expired'/'active' without mutating the shared default workspace."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO workspaces(name, slug, plan, plan_status, trial_ends_at) "
            "VALUES($1, $2, 'trial', $3, $4) RETURNING id",
            f"WS {uuid4().hex[:8]}", f"ws-{uuid4().hex[:8]}", plan_status, trial_ends_at,
        )


_INGEST = {"source": "meeting", "title": "Note", "text": "We chose Postgres."}


async def test_new_workspace_starts_trialing(pool):
    """Wizard-created workspace gets a 7-day trial and reports it via billing."""
    async with await _client() as client:
        await client.post(
            "/api/auth/register",
            json={
                "email": f"trial-{uuid4().hex}@example.test",
                "display_name": "Trial Founder",
                "password": "correct horse battery staple",
            },
        )
        await client.post("/api/workspace/create", json={"name": "Trial Co"})
        status = await client.get("/api/billing/status")
        assert status.status_code == 200
        body = status.json()
        assert body["plan_status"] == "trialing"
        assert body["writable"] is True
        assert body["days_left"] == 7
        assert body["trial_ends_at"] is not None


async def test_expired_trial_blocks_writes_but_allows_reads(pool):
    ws = await _workspace_with_billing(
        pool, "trialing", datetime.now(timezone.utc) - timedelta(days=1)
    )
    admin, _ = await _auth_client(pool, ws, role="admin")
    async with admin:
        # A mutating route is 402 (read-only)…
        blocked = await admin.post("/api/ingest", json=_INGEST)
        assert blocked.status_code == 402
        # …but reads stay open, and so do auth + billing.
        assert (await admin.get("/api/sources")).status_code == 200
        assert (await admin.get("/api/auth/me")).status_code == 200
        st = await admin.get("/api/billing/status")
        assert st.status_code == 200
        assert st.json()["plan_status"] == "expired"
        assert st.json()["writable"] is False
        assert st.json()["days_left"] == 0


async def test_query_blocked_when_workspace_read_only(pool):
    ws = await _workspace_with_billing(pool, "expired", None)
    member, _ = await _auth_client(pool, ws, role="member")
    async with member:
        resp = await member.post("/api/query", json={"question": "why postgres?"})
        assert resp.status_code == 402


async def test_checkout_activates_and_reenables_writes(pool, monkeypatch):
    monkeypatch.setattr(config, "BILLING_STUB_CHECKOUT", True)
    ws = await _workspace_with_billing(
        pool, "trialing", datetime.now(timezone.utc) - timedelta(days=1)
    )
    owner, _ = await _auth_client(pool, ws, role="owner")
    async with owner:
        assert (await owner.post("/api/ingest", json=_INGEST)).status_code == 402
        checkout = await owner.post("/api/billing/checkout")
        assert checkout.status_code == 200
        assert checkout.json() == {"activated": True, "url": None}
        # The next request re-reads plan_status='active' → writes flow again.
        assert (await owner.post("/api/ingest", json=_INGEST)).status_code == 200
        st = await owner.get("/api/billing/status")
        assert st.json()["plan_status"] == "active"
        assert st.json()["writable"] is True


async def test_checkout_disabled_by_default_does_not_grant_the_paid_plan(pool):
    """The stub activates a paid plan without taking payment, so it must be off
    unless explicitly opted into — otherwise any owner can skip the trial gate."""
    ws = await _workspace_with_billing(pool, "expired", None)
    owner, _ = await _auth_client(pool, ws, role="owner")
    async with owner:
        assert (await owner.post("/api/billing/checkout")).status_code == 501
        # ...and the workspace is still read-only afterwards.
        assert (await owner.post("/api/ingest", json=_INGEST)).status_code == 402
        assert (await owner.get("/api/billing/status")).json()["writable"] is False


async def test_formation_health_withholds_fleet_counts_without_a_token(pool):
    """The detailed payload counts documents and failures across EVERY workspace
    on the instance, so an anonymous caller gets liveness only."""
    async with await _client() as client:
        resp = await client.get("/api/health/formation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["detail"] is False
    assert body["status"] in ("ok", "stalled")
    for leaked in ("pending", "failed", "complete", "workers", "failed_1h"):
        assert leaked not in body


async def test_formation_health_returns_detail_with_a_valid_token(pool, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_TOKEN", "s3cret-health")
    async with await _client() as client:
        good = await client.get(
            "/api/health/formation", headers={"x-health-token": "s3cret-health"}
        )
        bad = await client.get(
            "/api/health/formation", headers={"x-health-token": "wrong"}
        )
    assert good.status_code == 200
    assert "pending" in good.json() and "workers" in good.json()
    assert bad.status_code == 401


async def test_oversized_request_body_is_rejected(pool, workspace_id):
    """Uvicorn imposes no body limit, so the middleware is the only thing between
    a huge POST and an LLM prompt. Declared Content-Length is refused up front."""
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        resp = await member.post(
            "/api/query",
            content=b"x" * (config.MAX_REQUEST_BYTES + 1),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413


async def test_oversized_chunked_body_is_cut_off(pool, workspace_id):
    """A chunked request declares no Content-Length, so it's metered as it
    streams. The abort surfaces as FastAPI's 400 rather than a 413 (see
    BodySizeLimitMiddleware) — what matters is that it's refused, not buffered."""
    member, _ = await _auth_client(pool, workspace_id, role="member")

    async def oversized_chunks():
        chunk = b"x" * 65536
        for _ in range(config.MAX_REQUEST_BYTES // len(chunk) + 5):
            yield chunk

    async with member:
        resp = await member.post(
            "/api/query",
            content=oversized_chunks(),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code in (400, 413)


async def test_overlong_question_is_rejected_by_field_cap(pool, workspace_id):
    """Under the body limit but over the per-field cap — Pydantic refuses it
    before any retrieval or LLM work starts."""
    member, _ = await _auth_client(pool, workspace_id, role="member")
    async with member:
        resp = await member.post(
            "/api/query", json={"question": "x" * (config.MAX_QUESTION_CHARS + 1)}
        )
        assert resp.status_code == 422


async def test_checkout_requires_owner(pool):
    ws = await _workspace_with_billing(pool, "expired", None)
    admin, _ = await _auth_client(pool, ws, role="admin")
    async with admin:
        assert (await admin.post("/api/billing/checkout")).status_code == 403


# ---- Account: sign-out-everywhere + leave workspace ----

async def test_me_reports_auth_provider(pool, workspace_id):
    client, _ = await _auth_client(pool, workspace_id, role="member")
    async with client:
        body = (await client.get("/api/auth/me")).json()
        assert body["user"]["auth_provider"] == "password"


async def test_logout_all_revokes_every_session(pool, workspace_id):
    client, user_id = await _auth_client(pool, workspace_id, role="admin")
    other_token = f"test-{uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth_sessions(user_id, workspace_id, token_hash, expires_at) "
            "VALUES($1, $2, $3, $4)",
            user_id, workspace_id, auth._hash_token(other_token),
            datetime.now(timezone.utc) + timedelta(days=1),
        )
    async with client:
        assert (await client.post("/api/auth/logout-all")).status_code == 200
    # the other device's session is now dead too
    other = await _client()
    other.cookies.set(auth.COOKIE_NAME, other_token, path="/")
    async with other:
        assert (await other.get("/api/auth/me")).status_code == 401


async def test_member_can_leave_and_session_repoints(pool, workspace_id):
    client, user_id = await _auth_client(pool, workspace_id, role="member")
    async with pool.acquire() as conn:
        ws2 = await conn.fetchval(
            "INSERT INTO workspaces(name, slug) VALUES($1, $2) RETURNING id",
            "Second WS", f"second-{uuid4().hex[:8]}",
        )
        await conn.execute(
            "INSERT INTO workspace_memberships(workspace_id, user_id, role) "
            "VALUES($1, $2, 'admin')",
            ws2, user_id,
        )
    async with client:
        resp = await client.post("/api/workspace/leave")
        assert resp.status_code == 200
        # session re-pointed to the remaining workspace
        assert resp.json()["workspace"]["id"] == ws2
    async with pool.acquire() as conn:
        gone = await conn.fetchval(
            "SELECT 1 FROM workspace_memberships WHERE workspace_id=$1 AND user_id=$2",
            workspace_id, user_id,
        )
    assert gone is None


async def test_sole_owner_cannot_leave(pool, workspace_id):
    owner, _ = await _auth_client(pool, workspace_id, role="owner")
    async with owner:
        resp = await owner.post("/api/workspace/leave")
        assert resp.status_code == 409


async def test_sources_reports_last_sync_documents(pool, workspace_id):
    """GET /api/sources surfaces the most recent completed sync's document count
    so the UI can show 'N imported' / 'nothing imported'."""
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO source_connections(workspace_id, provider, name, status, "
            "external_workspace_id, access_token_enc) "
            "VALUES($1, 'github', 'acme', 'connected', 'ext', 'enc') RETURNING id",
            workspace_id,
        )
        # older completed job imported 5; latest completed job imported 0
        await conn.execute(
            "INSERT INTO sync_jobs(workspace_id, connection_id, provider, status, kind, "
            "state, stats, completed_at) VALUES "
            "($1,$2,'github','complete','backfill','{}'::jsonb,'{\"documents\":5}'::jsonb, now() - interval '1 hour'),"
            "($1,$2,'github','complete','reconcile','{}'::jsonb,'{\"documents\":0}'::jsonb, now())",
            workspace_id, cid,
        )
    async with admin:
        resp = await admin.get("/api/sources")
        assert resp.status_code == 200
        conn_row = next(c for c in resp.json()["connections"] if c["id"] == cid)
    # reflects the *latest* completed job, not the older one
    assert conn_row["last_sync_documents"] == 0


async def test_sources_last_sync_documents_null_without_completed_job(pool, workspace_id):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO source_connections(workspace_id, provider, name, status, "
            "external_workspace_id, access_token_enc) "
            "VALUES($1, 'github', 'acme2', 'connected', 'ext2', 'enc') RETURNING id",
            workspace_id,
        )
    async with admin:
        resp = await admin.get("/api/sources")
        conn_row = next(c for c in resp.json()["connections"] if c["id"] == cid)
    assert conn_row["last_sync_documents"] is None


def _sse_event(body: str, event: str):
    """Pull the JSON payload of a named SSE event out of a response body."""
    for block in body.split("\n\n"):
        if any(line.strip() == f"event: {event}" for line in block.splitlines()):
            for line in block.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[len("data: "):])
    return None


class _FakeStream:
    def __init__(self, parts):
        self._parts = parts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    @property
    def text_stream(self):
        async def gen():
            for p in self._parts:
                yield p
        return gen()


async def _ask_with_meta(pool, workspace_id, monkeypatch, meta_json):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        await admin.post("/api/ingest", json={
            "source": "meeting", "title": "DB notes",
            "text": "The team decided to keep PostgreSQL for v1.",
        })
        parts = ["We kept Postgres [C1].", "\n<<<MEMORY_METADATA>>>\n", meta_json]
        monkeypatch.setattr(
            "app.providers.llm.stream_text", lambda *_a, **_k: _FakeStream(parts))
        resp = await admin.post("/api/query", json={"question": "what db?"})
        assert resp.status_code == 200
        return _sse_event(resp.text, "metadata")


async def test_query_citation_carries_precise_quote(pool, workspace_id, monkeypatch):
    meta = await _ask_with_meta(
        pool, workspace_id, monkeypatch,
        '{"confidence":"high","citations":[{"chunk_id":1,"quote":"keep PostgreSQL for v1"}],'
        '"related_questions":[],"timeline":[]}',
    )
    cites = meta["citations"]
    assert len(cites) == 1
    assert cites[0]["chunk_id"] == 1
    assert cites[0]["quote"] == "keep PostgreSQL for v1"  # verbatim span from the chunk


async def test_query_citation_quote_none_when_not_verbatim(pool, workspace_id, monkeypatch):
    meta = await _ask_with_meta(
        pool, workspace_id, monkeypatch,
        '{"confidence":"high","citations":[{"chunk_id":1,"quote":"a paraphrase not in the text"}],'
        '"related_questions":[],"timeline":[]}',
    )
    assert meta["citations"][0]["quote"] is None


async def test_query_citation_legacy_cited_chunk_ids_still_work(pool, workspace_id, monkeypatch):
    meta = await _ask_with_meta(
        pool, workspace_id, monkeypatch,
        '{"confidence":"high","cited_chunk_ids":[1],"related_questions":[],"timeline":[]}',
    )
    cites = meta["citations"]
    assert len(cites) == 1 and cites[0]["chunk_id"] == 1
    assert cites[0]["quote"] is None  # no quote in the old shape


async def test_query_unsupported_visible_citation_lowers_deterministic_confidence(
    pool, workspace_id, monkeypatch
):
    admin, _ = await _auth_client(pool, workspace_id, role="admin")
    async with admin:
        await admin.post("/api/ingest", json={
            "source": "meeting", "title": "DB notes",
            "text": "The team decided to keep PostgreSQL for v1.",
        })
        parts = [
            "We kept Postgres [C999].",
            "\n<<<MEMORY_METADATA>>>\n",
            '{"confidence":"high","citations":[{"chunk_id":999,"quote":"invented"}]}'
        ]
        monkeypatch.setattr(
            "app.providers.llm.stream_text", lambda *_a, **_k: _FakeStream(parts))
        resp = await admin.post("/api/query", json={"question": "what db?"})
        assert resp.status_code == 200
        meta = _sse_event(resp.text, "metadata")

    assert meta["confidence"] == "low"
    assert meta["verification"]["invalid_citation_ids"] == [999]
    assert meta["verification"]["citation_coverage"] == 0.0
