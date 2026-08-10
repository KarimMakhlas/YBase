"""Deterministic final-stage retrieval ranking contracts."""

from app.domains.query.retrieval import (
    classify_query_intent,
    relation_priority_for_intent,
    rerank_candidate_ids,
    select_diverse_chunks,
)


def test_reranker_rewards_candidates_supported_by_semantic_and_lexical_lists():
    ranked = rerank_candidate_ids(
        vector_ids=[10, 20, 30],
        text_ids=[40, 20, 50],
        limit=5,
    )

    assert ranked[0] == 20
    assert set(ranked) == {10, 20, 30, 40, 50}


def test_final_context_selector_diversifies_documents_before_repeating_one_source():
    chunks = [
        {"id": 1, "document_id": 100, "retrieval_score": 0.99, "text": "a"},
        {"id": 2, "document_id": 100, "retrieval_score": 0.98, "text": "b"},
        {"id": 3, "document_id": 200, "retrieval_score": 0.80, "text": "c"},
    ]

    selected = select_diverse_chunks(chunks, limit=2, per_document_cap=1)

    assert [chunk["id"] for chunk in selected] == [1, 3]


def test_query_intent_prioritizes_history_people_and_open_question_relations():
    history = relation_priority_for_intent(classify_query_intent("Why was the database choice reversed?"))
    people = relation_priority_for_intent(classify_query_intent("Who advocated for PostgreSQL?"))
    questions = relation_priority_for_intent(classify_query_intent("Which questions are still unanswered?"))

    assert history["revisits"] < history["about"]
    assert people["involves"] < people["revisits"]
    assert questions["resolves"] < questions["about"]
