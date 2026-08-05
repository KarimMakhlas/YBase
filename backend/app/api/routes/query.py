from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core import config
from app.core.ratelimit import query_limiter
from app.domains.auth import service as auth
from app.domains.query.streaming import stream_query

router = APIRouter(prefix="/api", tags=["query"])


class HistoryTurn(BaseModel):
    role: str
    # Truncated to the same cap the prompt builder applies per turn anyway.
    content: str = Field("", max_length=config.MAX_MESSAGE_CHARS)


class QueryRequest(BaseModel):
    # Bounded: the question goes straight into an LLM prompt, so an unbounded
    # field is unbounded token spend. History is capped per turn above and
    # trimmed to the last 6 turns downstream.
    question: str = Field(..., max_length=config.MAX_QUESTION_CHARS)
    history: Optional[List[HistoryTurn]] = Field(None, max_length=100)


@router.post("/query")
async def query(
    req: QueryRequest,
    current: auth.AuthContext = Depends(auth.require_writable_workspace("member")),
) -> StreamingResponse:
    if not req.question.strip():
        raise HTTPException(400, "question must not be empty")
    await query_limiter.enforce((current.workspace_id, current.user_id), "query")
    history = [t.model_dump() for t in (req.history or [])]
    return StreamingResponse(
        stream_query(req.question.strip(), workspace_id=current.workspace_id, history=history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
