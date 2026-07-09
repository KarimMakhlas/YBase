"""Pure-function coverage: chunking, hashing, JSON salvage, topics, scoring,
similarity pairing, and Slack event plumbing — no DB, no network."""

import time

from app.core import config
from app.domains.documents.ingestion import chunk_text, content_hash
from app.providers import llm
from app.providers.llm import parse_loose_json
from app.domains.query.streaming import _strip_metadata_bleed
from app.domains.query.retrieval import rank_graph_evidence, rrf_fuse
from app.domains.memory.consolidate import similar_pairs, similar_pairs_against
from app.domains.memory.formation import fallback_topics
from app.domains.memory.scoring import node_score
from app.providers.embeddings import _local_embed
from app.domains.connectors.slack.events import clean_text, thread_document, verify_signature, wanted_event
from app.domains.connectors.jira.client import issue_to_doc as jira_issue_to_doc
from app.domains.connectors.github.client import issue_to_doc as github_issue_to_doc
from app.domains.connectors.service import _frontend_from_request
from starlette.requests import Request

import hashlib
import hmac


# ---- chunking ----

def test_chunk_packs_paragraphs():
    text = "\n\n".join(["para " + "x" * 100] * 5)
    chunks = chunk_text(text, target=300, hard_max=1500)
    assert len(chunks) > 1
    assert all(len(c) <= 1500 for c in chunks)
    # nothing lost
    assert sum(c.count("para") for c in chunks) == 5


def test_chunk_hard_splits_monster_paragraph():
    text = "y" * 4000
    chunks = chunk_text(text, target=900, hard_max=1500)
    assert all(len(c) <= 1500 for c in chunks)
    assert sum(len(c) for c in chunks) == 4000


def test_chunk_empty_text_falls_back():
    assert chunk_text("") == [""]


def test_content_hash_distinguishes_and_repeats():
    a = content_hash("slack", "t", "body")
    assert a == content_hash("slack", "t", "body")
    assert a != content_hash("slack", "t", "body2")
    assert a != content_hash("notion", "t", "body")


def test_oauth_redirect_rejects_untrusted_referer(monkeypatch):
    monkeypatch.setattr(config, "APP_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(config, "CORS_ORIGINS", ["https://app.example.com"])

    allowed = Request({
        "type": "http",
        "headers": [(b"referer", b"https://app.example.com/sources?provider=slack")],
    })
    attacker = Request({
        "type": "http",
        "headers": [(b"referer", b"https://evil.example/steal")],
    })
    assert _frontend_from_request(allowed) == "https://app.example.com/sources"
    assert _frontend_from_request(attacker) == "https://app.example.com"


# ---- reciprocal-rank fusion ----

def test_rrf_overlap_outranks_single_list_winners():
    # 3 appears in both lists (mid-rank) and must beat either list's #1
    fused = rrf_fuse([[1, 3, 5], [2, 3, 6]], limit=6)
    assert fused[0] == 3
    assert set(fused) == {1, 2, 3, 5, 6}


def test_rrf_respects_limit_and_single_list_order():
    assert rrf_fuse([[7, 8, 9]], limit=2) == [7, 8]


def test_rrf_empty_lists():
    assert rrf_fuse([[], []], limit=5) == []


# ---- graph-evidence ranking ----

def test_rank_graph_evidence_blends_trust_and_relevance():
    score_by_node = {1: 1.0, 2: 0.25}  # e.g. a decided node vs a reversed one
    rows = [
        {"id": 100, "node_id": 2, "sim": 0.9},   # weak memory, relevant chunk
        {"id": 101, "node_id": 1, "sim": 0.9},   # strong memory, relevant chunk
        {"id": 102, "node_id": 1, "sim": 0.0},   # strong memory, unrelated chunk
    ]
    ranked = [r["id"] for r in rank_graph_evidence(rows, score_by_node)]
    assert ranked == [101, 102, 100]
    # the similarity floor keeps trusted-but-differently-worded memory in play
    assert ranked.index(102) < ranked.index(100)


def test_rank_graph_evidence_chunk_ranked_once_by_best_node():
    score_by_node = {1: 1.0, 2: 0.25}
    rows = [
        {"id": 100, "node_id": 2, "sim": 0.5},   # same chunk, weak node first
        {"id": 100, "node_id": 1, "sim": 0.5},   # …but its best node decides
        {"id": 101, "node_id": 2, "sim": 1.0},
    ]
    ranked = rank_graph_evidence(rows, score_by_node)
    assert [r["id"] for r in ranked] == [100, 101]


def test_rank_graph_evidence_handles_null_similarity():
    ranked = rank_graph_evidence([{"id": 7, "node_id": 9, "sim": None}], {})
    assert [r["id"] for r in ranked] == [7]


# ---- loose JSON salvage ----

def test_parse_loose_json_plain_and_fenced():
    assert parse_loose_json('{"a": 1}') == {"a": 1}
    assert parse_loose_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_loose_json('Sure! Here it is: {"a": 1} hope that helps') == {"a": 1}


def test_parse_loose_json_garbage_is_empty():
    assert parse_loose_json("no json here") == {}
    assert parse_loose_json("[1, 2, 3]") == {}


def test_strip_metadata_bleed_removes_card_sections():
    raw = (
        "PostgreSQL won because it fit the relational workload [C1].\n\n"
        "| Caveat | Supporting Chunk(s) |\n"
        "|---|---|\n"
        "| MongoDB was faster for writes | [C4] |\n\n"
        "---\n"
        "Thus, PostgreSQL was selected."
    )
    assert _strip_metadata_bleed(raw) == (
        "PostgreSQL won because it fit the relational workload [C1]."
    )

    raw = "PostgreSQL won because it fit the workload [C1].\n\nTimeline:\n- 2026-01-01 Chosen"
    assert _strip_metadata_bleed(raw) == "PostgreSQL won because it fit the workload [C1]."


def test_strip_metadata_bleed_repairs_compact_citations_and_hides_graph_ids():
    raw = "PostgreSQL stayed the default. C199C204C208[N908]"
    assert _strip_metadata_bleed(raw) == (
        "PostgreSQL stayed the default. [C199] [C204] [C208]"
    )


def test_auto_provider_uses_nvidia_before_ollama(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(config, "NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setattr(config, "NVIDIA_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setattr(llm, "anthropic_credentials_available", lambda: False)

    assert llm.active_provider() == "nvidia"
    assert llm.active_model() == "openai/gpt-oss-120b"
    assert llm.credentials_available()


# ---- topic fallback ----

def test_fallback_topics_prefers_doc_tags():
    assert fallback_topics("Anything at all", ["Database", " scaling "]) == ["database", "scaling"]


def test_fallback_topics_mines_title():
    topics = fallback_topics("Move CI from CircleCI to GitHub Actions", [])
    assert topics and all(t.islower() for t in topics)
    assert "the" not in topics


def test_fallback_topics_never_empty():
    assert fallback_topics("a an the", []) == ["general"]


# ---- confidence scoring ----

def test_score_recent_decided_beats_old_reversed():
    recent = node_score("decided", {"date": "2026-06-01"}, evidence_count=2)
    old_reversed = node_score("reversed", {"date": "2024-01-01"}, evidence_count=2)
    assert recent > old_reversed


def test_score_more_evidence_scores_higher():
    one = node_score("decided", {"date": "2026-06-01"}, evidence_count=1)
    three = node_score("decided", {"date": "2026-06-01"}, evidence_count=3)
    assert three > one


def test_score_handles_missing_dates():
    s = node_score("open", {}, evidence_count=1)
    assert 0.0 < s <= 1.0


# ---- duplicate-decision pairing ----

def test_similar_pairs_flags_paraphrase_keeps_older():
    a = (10, _local_embed("Use PostgreSQL as the primary database for v1"))
    b = (20, _local_embed("Use PostgreSQL as primary database for v1"))
    c = (30, _local_embed("Adopt Redis cache for dashboard aggregates"))
    pairs = similar_pairs([b, a, c], threshold=0.8)
    assert (10, 20) in [(k, d) for k, d, _ in pairs]
    assert all({k, d} != {10, 30} and {k, d} != {20, 30} for k, d, _ in pairs)


def test_similar_pairs_below_threshold_empty():
    a = (1, _local_embed("completely unrelated topic about kubernetes"))
    b = (2, _local_embed("quarterly marketing budget review"))
    assert similar_pairs([a, b], threshold=0.8) == []


def test_similar_pairs_against_compares_targets_only():
    a = (10, _local_embed("Use PostgreSQL as the primary database for v1"))
    b = (20, _local_embed("Use PostgreSQL as primary database for v1"))
    c = (30, _local_embed("Adopt Redis cache for dashboard aggregates"))
    d = (40, _local_embed("Adopt Redis cache for dashboard aggregates"))  # exact dup of c
    # only b is a target: the a-b duplicate is found, the untouched c-d
    # duplicate is not compared at all — that's what makes it incremental
    pairs = similar_pairs_against([b], [a, b, c, d], threshold=0.8)
    ids = [(k, drop) for k, drop, _ in pairs]
    assert (10, 20) in ids
    assert (30, 40) not in ids
    assert all(k != drop for k, drop in ids)  # no self-pairs


# ---- slack ----

def _sign(secret: str, ts: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256)
    return f"v0={digest.hexdigest()}"


def test_slack_signature_roundtrip():
    secret, body = "shhh", b'{"type":"event_callback"}'
    ts = str(time.time())
    assert verify_signature(secret, ts, body, _sign(secret, ts, body))
    assert not verify_signature(secret, ts, body, _sign("wrong", ts, body))


def test_slack_signature_rejects_replay():
    secret, body = "shhh", b"{}"
    old_ts = str(time.time() - 600)
    assert not verify_signature(secret, old_ts, body, _sign(secret, old_ts, body))


def test_slack_clean_text():
    raw = "<@U123> see <https://x.test/doc|the doc> &amp; <https://y.test>"
    assert clean_text(raw) == "@U123 see the doc & https://y.test"


def test_slack_wanted_event_filters():
    assert wanted_event({"type": "message", "text": "hi", "channel": "C1"})
    assert not wanted_event({"type": "message", "subtype": "channel_join", "text": "x"})
    assert not wanted_event({"type": "reaction_added", "text": "x"})
    assert not wanted_event({"type": "message", "text": "   "})


def test_slack_thread_document_shape():
    from datetime import datetime, timezone
    msgs = [
        {"user_id": "U1", "text": "should we switch to GHA?", "event_at": datetime(2026, 1, 2, tzinfo=timezone.utc)},
        {"user_id": "U2", "text": "yes, CircleCI costs too much", "event_at": datetime(2026, 1, 2, 1, tzinfo=timezone.utc)},
    ]
    doc = thread_document("C042", msgs, stream_name="eng-infra", external_ref="slack:T1:C042:1")
    assert doc["source"] == "slack"
    assert doc["title"].startswith("#eng-infra thread:")
    assert "U1: should we switch" in doc["text"]
    assert doc["author"] == "U1"
    assert doc["created_at"].startswith("2026-01-02")
    assert doc["external_ref"] == "slack:T1:C042:1"


# ---- connector document mapping ----

def test_jira_issue_to_doc_shape():
    connection = {"id": 10, "external_workspace_id": "cloud-1"}
    stream = {"id": 20, "name": "Platform"}
    issue = {
        "key": "PLAT-42",
        "fields": {
            "summary": "Move billing jobs to the worker queue",
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Queueing avoids API timeouts."}]}
                ],
            },
            "comment": {"comments": []},
            "reporter": {"displayName": "Maya"},
            "status": {"name": "Done"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "High"},
            "labels": ["billing"],
            "created": "2026-01-15T09:30:00.000+0000",
        },
    }
    doc = jira_issue_to_doc(connection, stream, issue)
    assert doc.source == "jira"
    assert doc.title.startswith("[PLAT-42]")
    assert "Queueing avoids API timeouts." in doc.text
    assert doc.author == "Maya"
    assert doc.external_ref == "jira:cloud-1:PLAT-42"


async def test_github_issue_to_doc_shape_without_comments():
    connection = {"id": 11}
    stream = {"id": 21, "external_id": "ybase/app"}
    issue = {
        "number": 7,
        "title": "Adopt persisted sessions",
        "state": "closed",
        "user": {"login": "mav"},
        "labels": [{"name": "backend"}],
        "body": "Persist sessions so Ask Memory history survives reloads.",
        "comments": 0,
        "created_at": "2026-02-01T12:00:00Z",
    }
    doc = await github_issue_to_doc("token-unused", connection, stream, issue)
    assert doc.source == "github"
    assert doc.title == "ybase/app#7: Adopt persisted sessions"
    assert "Persist sessions" in doc.text
    assert doc.author == "mav"
    assert doc.external_ref == "github:ybase/app:issue/7"


# ---- per-stream re-sync lookback resolver ----

from app.domains.connectors import stream_lookback_days


def test_lookback_full_backfill_for_never_synced_stream():
    assert stream_lookback_days({}, None) == config.CONNECTOR_BACKFILL_DAYS
    assert stream_lookback_days(None, None) == config.CONNECTOR_BACKFILL_DAYS


def test_lookback_short_window_for_already_synced_stream():
    assert stream_lookback_days({}, "2026-01-01T00:00:00Z") == config.CONNECTOR_RESYNC_WINDOW_DAYS


def test_lookback_explicit_days_overrides_per_stream_logic():
    # the manual "Backfill N days" button / Slack reconcile set state.days
    assert stream_lookback_days({"days": 30}, None) == 30
    assert stream_lookback_days({"days": 30}, "2026-01-01T00:00:00Z") == 30


# ---- citation quote location (sentence-precise citations) ----

from app.domains.query.streaming import locate_quote


def test_locate_quote_exact_substring():
    chunk = "The billing model is relational. We need real transactions there."
    assert locate_quote(chunk, "We need real transactions there.") == "We need real transactions there."


def test_locate_quote_whitespace_tolerant_maps_back_to_original():
    # the model re-flowed the newline into a space when copying the quote
    chunk = "The billing model is\nrelational and joined."
    got = locate_quote(chunk, "billing model is relational and joined.")
    assert got == "billing model is\nrelational and joined."  # original span, verbatim


def test_locate_quote_not_present_returns_none():
    assert locate_quote("We chose Postgres for v1.", "We chose MongoDB instead") is None


def test_locate_quote_empty_inputs_return_none():
    assert locate_quote("some text", None) is None
    assert locate_quote("some text", "   ") is None
    assert locate_quote("", "text") is None


# ---- rate limiting / structured logging ----

def test_sliding_window_limiter_drops_over_budget_per_key():
    from app.core.ratelimit import SlidingWindowLimiter
    lim = SlidingWindowLimiter(max_events=3, window_s=60)
    assert [lim.allow("team-a") for _ in range(3)] == [True, True, True]
    assert lim.allow("team-a") is False        # 4th within the window is dropped
    assert lim.allow("team-b") is True         # a different key is unaffected


def test_sliding_window_limiter_zero_disables():
    from app.core.ratelimit import SlidingWindowLimiter
    lim = SlidingWindowLimiter(max_events=0)
    assert all(lim.allow("k") for _ in range(1000))


def test_json_log_formatter_emits_valid_json_with_request_id():
    import json
    import logging
    from app.core.observability import _JsonFormatter
    rec = logging.LogRecord("whybase.test", logging.INFO, __file__, 1,
                            "hello %s", ("world",), None)
    rec.request_id = "rid-123"
    out = json.loads(_JsonFormatter().format(rec))
    assert out["msg"] == "hello world"
    assert out["request_id"] == "rid-123"
    assert out["level"] == "INFO"
    assert out["logger"] == "whybase.test"


# ---- trusted client IP (Item 11) ----

class _FakeRequest:
    def __init__(self, headers, host="10.0.0.1"):
        self.headers = headers  # lowercase keys; matches how _client_ip looks up
        self.client = type("C", (), {"host": host})()


def test_client_ip_uses_rightmost_xff_not_spoofable_first():
    from app.domains.auth.service import _client_ip
    from app.core import config
    # leftmost entry is client-supplied; the rightmost is what the trusted proxy saw
    r = _FakeRequest({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
    saved = config.REAL_IP_HEADER
    try:
        config.REAL_IP_HEADER = ""
        assert _client_ip(r) == "3.3.3.3"
        assert _client_ip(_FakeRequest({})) == "10.0.0.1"  # falls back to socket peer
        # a configured platform header (set by the proxy) wins outright
        config.REAL_IP_HEADER = "fly-client-ip"
        r2 = _FakeRequest({"fly-client-ip": "8.8.8.8", "x-forwarded-for": "1.1.1.1"})
        assert _client_ip(r2) == "8.8.8.8"
    finally:
        config.REAL_IP_HEADER = saved


# ---- connector secret encryption + key rotation (Item 13) ----

def test_connector_secret_roundtrip_and_rotation(monkeypatch):
    from app.core import config, crypto
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEY", "key-one")
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEYS_OLD", [])
    token = crypto.encrypt_secret("xoxb-secret")
    assert ":" in token  # tagged with the key id
    assert crypto.decrypt_secret(token) == "xoxb-secret"
    # rotate: new key primary, old key retained -> old ciphertext still decrypts
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEY", "key-two")
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEYS_OLD", ["key-one"])
    assert crypto.decrypt_secret(token) == "xoxb-secret"      # via retained old key
    new_token = crypto.encrypt_secret("xoxb-secret")          # tagged with key-two
    assert new_token != token
    assert crypto.decrypt_secret(new_token) == "xoxb-secret"


def test_connector_secret_decrypts_legacy_untagged(monkeypatch):
    from cryptography.fernet import Fernet
    from app.core import config, crypto
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEY", "legacy-key")
    monkeypatch.setattr(config, "CONNECTOR_SECRET_KEYS_OLD", [])
    legacy = Fernet(crypto._fernet_key("legacy-key")).encrypt(b"old-token").decode()
    assert ":" not in legacy  # the old format had no key tag
    assert crypto.decrypt_secret(legacy) == "old-token"


# ---- Slack reconcile gap (Item 14) ----

def test_slack_reconcile_days_spans_outage_gap(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.core import config
    from app.domains.connectors import slack_reconcile_days
    monkeypatch.setattr(config, "SLACK_RECONCILE_WINDOW_DAYS", 1)
    monkeypatch.setattr(config, "CONNECTOR_BACKFILL_DAYS", 90)
    now = datetime(2026, 6, 23, tzinfo=timezone.utc)
    assert slack_reconcile_days(None, now=now) == 1                       # never synced
    assert slack_reconcile_days(now - timedelta(hours=2), now=now) == 1   # recent
    assert slack_reconcile_days(now - timedelta(days=10), now=now) == 11  # outage widens
    assert slack_reconcile_days(now - timedelta(days=500), now=now) == 90 # capped
