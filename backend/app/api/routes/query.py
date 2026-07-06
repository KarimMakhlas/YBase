from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.ratelimit import query_limiter
from app.domains.auth import service as auth
from app.domains.query.streaming import stream_query

router = APIRouter(prefix="/api", tags=["query"])


class HistoryTurn(BaseModel):
    role: str
    content: str


class QueryRequest(BaseModel):
    question: str
    history: Optional[List[HistoryTurn]] = None


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
