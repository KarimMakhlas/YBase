"""Per-workspace LLM/embedding usage accounting.

Providers are instrumented at the call site (providers/llm.py,
providers/embeddings.py); attribution comes from a contextvar set at each
pipeline entry point (worker formation runs, consolidation, ingest, the query
stream) — the same propagation pattern as observability.request_id_var.
Recording is strictly best-effort: a metrics failure must never fail or slow
the work being metered. Token fields are None wherever a provider doesn't
report usage; request_count always counts.
"""

import logging
from contextvars import ContextVar, Token
from typing import Any, Dict, Optional

from . import db

log = logging.getLogger("ybase.usage")

usage_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "usage_context", default=None
)


def set_context(
    workspace_id: Optional[int] = None,
    surface: str = "unknown",
    document_id: Optional[int] = None,
) -> Token:
    """Attribute subsequent provider calls on this async path. Returns a token
    for reset_context — pair them in try/finally."""
    return usage_context.set(
        {"workspace_id": workspace_id, "surface": surface, "document_id": document_id}
    )


def reset_context(token: Token) -> None:
    usage_context.reset(token)


async def record(
    kind: str,
    provider: str,
    model: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    request_count: int = 1,
) -> None:
    ctx = usage_context.get() or {}
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO usage_events(workspace_id, surface, kind, provider, "
                "model, input_tokens, output_tokens, total_tokens, request_count, "
                "document_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                ctx.get("workspace_id"), ctx.get("surface") or "unknown", kind,
                provider, model, input_tokens, output_tokens, total_tokens,
                request_count, ctx.get("document_id"),
            )
    except Exception:
        log.warning("usage record failed (kind=%s provider=%s)", kind, provider)


# ── Pure payload parsers (unit-testable without network) ────────────────────


def usage_from_anthropic(msg: Any) -> Dict[str, Optional[int]]:
    u = getattr(msg, "usage", None)
    return {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
    }


def usage_from_openai_payload(data: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    u = (data or {}).get("usage") or {}
    return {
        "input_tokens": u.get("prompt_tokens"),
        "output_tokens": u.get("completion_tokens"),
    }


def usage_from_ollama_payload(data: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    d = data or {}
    return {
        "input_tokens": d.get("prompt_eval_count"),
        "output_tokens": d.get("eval_count"),
    }


def usage_from_voyage_payload(data: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    u = (data or {}).get("usage") or {}
    return {"total_tokens": u.get("total_tokens")}
