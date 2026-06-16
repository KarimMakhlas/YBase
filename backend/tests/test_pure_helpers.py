"""Pure-function coverage: chunking, hashing, JSON salvage, topics, scoring,
similarity pairing, and Slack event plumbing — no DB, no network."""

import time

from app.domains.documents.ingestion import chunk_text, content_hash
from app.providers.llm import parse_loose_json
from app.domains.query.retrieval import rrf_fuse
from app.domains.memory.consolidate import similar_pairs
from app.domains.memory.formation import fallback_topics
from app.domains.memory.scoring import node_score
from app.providers.embeddings import _local_embed
from app.domains.connectors.slack.events import clean_text, thread_document, verify_signature, wanted_event
from app.domains.connectors.jira.client import issue_to_doc as jira_issue_to_doc
from app.domains.connectors.github.client import issue_to_doc as github_issue_to_doc

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


# ---- loose JSON salvage ----

def test_parse_loose_json_plain_and_fenced():
    assert parse_loose_json('{"a": 1}') == {"a": 1}
    assert parse_loose_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_loose_json('Sure! Here it is: {"a": 1} hope that helps') == {"a": 1}


def test_parse_loose_json_garbage_is_empty():
    assert parse_loose_json("no json here") == {}
    assert parse_loose_json("[1, 2, 3]") == {}


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
    stream = {"id": 21, "external_id": "whybase/app"}
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
    assert doc.title == "whybase/app#7: Adopt persisted sessions"
    assert "Persist sessions" in doc.text
    assert doc.author == "mav"
    assert doc.external_ref == "github:whybase/app:issue/7"
