"""Extraction-output validation (capture-only).

_persist already repairs bad LLM output silently: safe_node() drops
cross-references to node ids that don't exist, and fallback_topics() invents
topics when the model returns none. Those repairs keep the graph sound but
hide quality drift — a model change that starts hallucinating node ids looks
identical to a healthy one. This module counts what was repaired, per run;
the worker stores the report on formation_runs.validation and the analytics
quality endpoint aggregates it. Pure — no DB, fully unit-testable.
"""

from typing import Any, Dict, Iterable, List, Optional, Set

_DETAIL_CAP = 10  # keep per-run reports small; counts carry the signal


def _bad_refs(item: Dict[str, Any], keys_single: List[str], key_multi: str,
              valid: Set[int]) -> List[int]:
    bad = []
    for key in keys_single:
        ref = item.get(key)
        if ref is not None and ref not in valid:
            bad.append(ref)
    for ref in item.get(key_multi) or []:
        if ref not in valid:
            bad.append(ref)
    return bad


def validate_extraction(
    result: Dict[str, Any],
    valid_node_ids: Set[int],
    chunk_indexes: Iterable[int],
    min_reasoning_chars: int = 40,
) -> Dict[str, Any]:
    """Count repair-worthy defects in one extraction. Returns a flat report:
    counts per defect class, a capped detail list, and `flagged` when any
    class fired. Enforcement stays in _persist — this only observes."""
    chunk_set = set(chunk_indexes)
    decisions = result.get("decisions") or []
    entities = result.get("entities") or []
    questions = result.get("questions") or []

    invalid_cross_refs = 0
    empty_topics = 0
    trivial_reasoning = 0
    invalid_evidence_indexes = 0
    details: List[str] = []

    def note(msg: str) -> None:
        if len(details) < _DETAIL_CAP:
            details.append(msg)

    for dec in decisions:
        title = (dec.get("title") or "?")[:80]
        bad = _bad_refs(dec, ["revisits_node_id", "resolves_question_node_id"],
                        "relates_to_node_ids", valid_node_ids)
        if bad:
            invalid_cross_refs += len(bad)
            note(f"decision '{title}': unknown node refs {bad}")
        if not [t for t in dec.get("topics") or [] if str(t).strip()]:
            empty_topics += 1
            note(f"decision '{title}': no topics (fallback used)")
        reasoning = (dec.get("reasoning") or "").strip()
        if len(reasoning) < min_reasoning_chars or reasoning == (dec.get("what") or "").strip():
            trivial_reasoning += 1
            note(f"decision '{title}': trivial reasoning ({len(reasoning)} chars)")

    for q in questions:
        label = (q.get("question") or "?")[:80]
        bad = _bad_refs(q, ["resolves_node_id"], "relates_to_node_ids", valid_node_ids)
        if bad:
            invalid_cross_refs += len(bad)
            note(f"question '{label}': unknown node refs {bad}")

    for item in [*decisions, *entities, *questions]:
        bad_idx = [i for i in item.get("evidence_chunk_indexes") or [] if i not in chunk_set]
        if bad_idx:
            invalid_evidence_indexes += len(bad_idx)
            note(f"evidence indexes outside the document: {bad_idx}")

    empty_extraction = not (decisions or entities or questions)
    report = {
        "invalid_cross_refs": invalid_cross_refs,
        "empty_topics": empty_topics,
        "trivial_reasoning": trivial_reasoning,
        "invalid_evidence_indexes": invalid_evidence_indexes,
        "empty_extraction": empty_extraction,
    }
    report["flagged"] = bool(
        invalid_cross_refs or empty_topics or trivial_reasoning
        or invalid_evidence_indexes or empty_extraction
    )
    if details:
        report["details"] = details
    return report
