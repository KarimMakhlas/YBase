"""HTTP-level coverage for auth, protected routes, and query SSE."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from app.core import db
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
            auth.hash_password("correct horse battery staple"),
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
    await db.init_schema()

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
            email, "Forgot User", auth.hash_password("correct horse battery staple"),
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


async def test_reset_password_consumes_token_and_revokes_sessions(pool, workspace_id):
    email = f"reset-{uuid4().hex}@example.test"
    raw = f"reset-{uuid4().hex}"
    sess = f"sess-{uuid4().hex}"
    async with pool.acquire() as conn:
        user_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) VALUES($1, $2, $3) "
            "RETURNING id",
            email, "Reset User", auth.hash_password("the old password here"),
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
            email, "Expired User", auth.hash_password("correct horse battery staple"),
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


async def test_google_callback_links_existing_password_account(pool, monkeypatch):
    """Signing in with Google on an email that already has a password account
    links to that same user (no duplicate) and stamps google_sub. Matching is
    case-insensitive on email."""
    email = f"glink-{uuid4().hex}@example.test"
    async with pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "INSERT INTO users(email, display_name, password_hash) "
            "VALUES($1, $2, $3) RETURNING id",
            email, "Pat Password", auth.hash_password("correct horse battery staple"),
        )
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
