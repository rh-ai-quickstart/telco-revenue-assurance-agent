from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..models import SessionResponse, TurnRequest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat")


@router.post("/sessions", response_model=SessionResponse)
async def create_session(request: Request):
    runner = request.app.state.runner
    session_id = runner.create_session()
    return SessionResponse(session_id=session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request):
    request.app.state.runner.delete_session(session_id)


@router.post("/sessions/{session_id}/turns")
async def chat_turn(session_id: str, body: TurnRequest, request: Request):
    runner = request.app.state.runner
    if session_id not in runner._sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        try:
            async for event in runner.stream_turn(session_id, body.message):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            log.exception("Streaming error for session %s", session_id)
            yield f"data: {json.dumps({'type': 'error', 'content': 'Internal server error'})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
