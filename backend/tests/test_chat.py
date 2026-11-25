"""
Unit tests for backend/routes/chat.py.

Route functions are called directly with a mocked Request object.
No ASGI stack or lifespan required — no real DB or OGX connection needed.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("OGX_URL", "http://localhost:8321")


def _mock_request(runner) -> MagicMock:
    req = MagicMock()
    req.app.state.runner = runner
    return req


async def _fake_stream(*events):
    for e in events:
        yield e


# ── Session management ────────────────────────────────────────────────────────

async def test_create_session_returns_session_id():
    from backend.routes.chat import create_session

    runner = MagicMock()
    runner.create_session.return_value = "new-session-id"

    result = await create_session(_mock_request(runner))

    assert result.session_id == "new-session-id"
    runner.create_session.assert_called_once()


async def test_delete_session_calls_runner():
    from backend.routes.chat import delete_session

    runner = MagicMock()
    await delete_session("session-123", _mock_request(runner))
    runner.delete_session.assert_called_once_with("session-123")


# ── Chat turn ─────────────────────────────────────────────────────────────────

async def test_chat_turn_unknown_session_returns_404():
    from fastapi import HTTPException
    from backend.routes.chat import chat_turn
    from backend.models import TurnRequest

    runner = MagicMock()
    runner._sessions = {}

    with pytest.raises(HTTPException) as exc_info:
        await chat_turn("no-such-session", TurnRequest(message="hello"), _mock_request(runner))

    assert exc_info.value.status_code == 404


async def test_chat_turn_returns_streaming_response():
    from fastapi.responses import StreamingResponse
    from backend.routes.chat import chat_turn
    from backend.models import TurnRequest

    runner = MagicMock()
    runner._sessions = {"s1": []}
    # stream_turn must be a regular callable returning an async generator (not AsyncMock)
    runner.stream_turn = MagicMock(return_value=_fake_stream(
        {"type": "text", "content": "Customer C001 is at risk."},
        {"type": "tool_call", "name": "predict_churn", "arguments": {}, "result": []},
    ))

    response = await chat_turn("s1", TurnRequest(message="check churn"), _mock_request(runner))

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"

    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(chunks)
    events = [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data:") and line != "data: [DONE]"
    ]
    types = {e["type"] for e in events}
    assert "text" in types
    assert "tool_call" in types
