"""
Unit tests for agent/runner.py.

AgentRunner is instantiated via __new__ to skip __init__ (which connects to OGX).
Only session management and tool dispatch are tested here — LLM calls are mocked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_runner():
    from agent.runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)
    runner._sessions = {}
    runner._client = MagicMock()
    return runner


# ── Session management ────────────────────────────────────────────────────────

class TestSessionManagement:
    def test_create_session_returns_uuid_string(self):
        runner = _make_runner()
        sid = runner.create_session()
        assert isinstance(sid, str) and len(sid) == 36

    def test_create_session_initialises_empty_history(self):
        runner = _make_runner()
        sid = runner.create_session()
        assert runner._sessions[sid] == []

    def test_delete_session_removes_history(self):
        runner = _make_runner()
        sid = runner.create_session()
        runner.delete_session(sid)
        assert sid not in runner._sessions

    def test_delete_unknown_session_is_silent(self):
        runner = _make_runner()
        runner.delete_session("does-not-exist")  # must not raise


# ── stream_turn error cases ───────────────────────────────────────────────────

class TestStreamTurn:
    def test_unknown_session_raises_value_error(self):
        runner = _make_runner()

        async def _run():
            async for _ in runner.stream_turn("no-such-session", "hello"):
                pass

        with pytest.raises(ValueError, match="Unknown session"):
            asyncio.run(_run())

    def test_yields_text_events_from_llm(self):
        runner = _make_runner()
        sid = runner.create_session()

        # _call_llm returns (tool_calls=[], text="Hello!")
        async def _run():
            with patch.object(runner, "_call_llm", new=AsyncMock(return_value=([], "Hello!"))):
                return [e async for e in runner.stream_turn(sid, "hi")]

        events = asyncio.run(_run())
        assert any(e["type"] == "text" and "Hello!" in e["content"] for e in events)

    def test_tool_call_is_yielded_and_result_added_to_history(self):
        runner = _make_runner()
        sid = runner.create_session()

        tool_call = {"call_id": "abc", "name": "predict_churn", "arguments": {"customer_ids": ["C001"]}}
        churn_result = [{"customer_id": "C001", "label": 1, "verdict": "At Risk", "churn_probability": 0.87}]

        # First _call_llm returns a tool call; second returns plain text (loop ends)
        call_llm_responses = [
            ([tool_call], ""),
            ([], "Customer C001 is at risk."),
        ]

        async def _run():
            with patch.object(runner, "_call_llm", new=AsyncMock(side_effect=call_llm_responses)), \
                 patch.object(runner, "_execute_tool", new=AsyncMock(return_value=churn_result)):
                return [e async for e in runner.stream_turn(sid, "check churn for C001")]

        events = asyncio.run(_run())
        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "predict_churn"
        assert tool_events[0]["result"] == churn_result


# ── _execute_tool ─────────────────────────────────────────────────────────────

class TestExecuteTool:
    def test_unknown_tool_returns_error_dict(self):
        runner = _make_runner()

        result = asyncio.run(runner._execute_tool("nonexistent_tool", {}))
        assert "error" in result

    def test_dispatches_to_correct_tool_function(self):
        runner = _make_runner()

        fake_result = [{"label": 0}]
        with patch("agent.runner.TOOL_MAP", {"predict_churn": MagicMock(return_value=fake_result)}):
            result = asyncio.run(runner._execute_tool("predict_churn", {"customer_ids": ["C001"]}))

        assert result == fake_result
