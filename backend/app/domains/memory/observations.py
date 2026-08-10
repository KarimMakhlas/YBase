"""Immutable extraction observations, kept separate from graph projection."""

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import asyncpg


PROMPT_VERSION = "formation-v1"


@dataclass(frozen=True)
class ObservationBatch:
    run_id: int
    valid_result: Dict[str, Any]


def _effective_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.combine(
            datetime.fromisoformat(str(value)).date(), time.min, tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _items(result: Mapping[str, Any]) -> Iterable[Tuple[str, int, Dict[str, Any]]]:
    ordinal = 0
    for kind, key in (("decision", "decisions"), ("entity", "entities"), ("question", "questions")):
        for item in result.get(key) or []:
            yield kind, ordinal, item
            ordinal += 1


def _evidence_reason(item: Mapping[str, Any], valid_indexes: set[int]) -> Optional[str]:
    indexes = item.get("evidence_chunk_indexes") or []
    if not indexes:
        return "missing evidence chunk indexes"
    invalid = sorted({index for index in indexes if index not in valid_indexes})
    if invalid:
        return f"invalid evidence chunk indexes: {invalid}"
    return None


async def create_candidate_run(
    conn: asyncpg.Connection,
    document: asyncpg.Record,
    *,
    model_provider: str,
    model_name: str,
    validation: Optional[Dict[str, Any]] = None,
    prompt_version: str = PROMPT_VERSION,
) -> int:
    """Create a non-active candidate tied to an immutable document revision."""
    revision_id = document["revision_id"]
    if revision_id is None:
        raise RuntimeError(f"document {document['id']} has no immutable revision")
    return await conn.fetchval(
        "INSERT INTO formation_runs(workspace_id, document_id, revision_id, status, "
        "llm_provider, llm_model, prompt_version, validation) "
        "VALUES($1, $2, $3, 'candidate', $4, $5, $6, $7) RETURNING id",
        document["workspace_id"], document["id"], revision_id,
        model_provider, model_name, prompt_version, validation or {},
    )


async def persist_observations(
    conn: asyncpg.Connection,
    run_id: int,
    document: asyncpg.Record,
    chunks: Sequence[asyncpg.Record],
    result: Dict[str, Any],
    *,
    model_provider: str,
    model_name: str,
    prompt_version: str = PROMPT_VERSION,
) -> ObservationBatch:
    """Persist every proposal; only evidence-complete proposals may be projected."""
    chunk_by_index = {chunk["chunk_index"]: chunk["id"] for chunk in chunks}
    valid_result = {**result, "decisions": [], "entities": [], "questions": []}
    target_key = {"decision": "decisions", "entity": "entities", "question": "questions"}

    for kind, ordinal, item in _items(result):
        reason = _evidence_reason(item, set(chunk_by_index))
        status = "quarantined" if reason else "valid"
        observation_id = await conn.fetchval(
            "INSERT INTO memory_observations(formation_run_id, workspace_id, document_id, "
            "revision_id, kind, ordinal, payload, confidence, effective_at, model_provider, "
            "model_name, prompt_version, status, quarantine_reason) "
            "VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) RETURNING id",
            run_id, document["workspace_id"], document["id"], document["revision_id"],
            kind, ordinal, item, float(item.get("confidence", 1.0)),
            _effective_at(item.get("date")), model_provider, model_name, prompt_version,
            status, reason,
        )
        if reason:
            continue
        for index in item.get("evidence_chunk_indexes") or []:
            await conn.execute(
                "INSERT INTO observation_evidence(workspace_id, observation_id, chunk_id) "
                "VALUES($1, $2, $3)",
                document["workspace_id"], observation_id, chunk_by_index[index],
            )
        # The graph projection must use this durable observation as its exact
        # provenance source. Keep the id only in the in-memory compatibility
        # projection payload; the immutable JSON stored above remains exactly
        # what the model proposed.
        valid_result[target_key[kind]].append({**item, "_observation_id": observation_id})

    return ObservationBatch(run_id=run_id, valid_result=valid_result)
