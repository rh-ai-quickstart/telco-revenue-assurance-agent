"""
OGX agentic loop — OpenAI-compatible client.

Uses the OpenAI-compatible API exposed by OGX (/v1/chat/completions) with a
client-side tool execution loop.  Sessions are kept in-process; replace
_sessions with a PostgreSQL-backed store for multi-replica deployments.

Event dict schema yielded by stream_turn():
  {"type": "text",        "content": str}
  {"type": "tool_call",   "name": str, "arguments": dict, "result": any}
  {"type": "error",       "content": str}
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator

import ast

from openai import OpenAI, BadRequestError

from . import tools as _tools
from .config import OGX_MODEL_ID, OGX_URL, SYSTEM_PROMPT, TOOL_DEFINITIONS

log = logging.getLogger(__name__)

TOOL_MAP: dict[str, Any] = {
    "query_db": _tools.query_db,
    "cross_metric_count": _tools.cross_metric_count,
    "count_customers": _tools.count_customers,
    "list_customers": _tools.list_customers,
    "fetch_customer_data": _tools.fetch_customer_data,
    "fetch_transaction_data": _tools.fetch_transaction_data,
    "predict_churn": _tools.predict_churn,
    "predict_fraud": _tools.predict_fraud,
    "predict_qoe": _tools.predict_qoe,
}

_SYSTEM_MESSAGE = {"role": "system", "content": SYSTEM_PROMPT}
_MAX_TOOL_ITERS = 15


def _to_openai_tools(defs: list[dict]) -> list[dict]:
    """Convert LlamaStack-format tool definitions to OpenAI function-calling format."""
    def _schema(param_type: str) -> dict:
        if param_type.startswith("list["):
            return {"type": "array", "items": {"type": "string"}}
        if param_type == "list":
            return {"type": "array"}
        return {"type": "string"}

    result = []
    for d in defs:
        props: dict = {}
        required: list[str] = []
        for pname, pmeta in d.get("parameters", {}).items():
            schema = _schema(pmeta.get("param_type", "string"))
            schema["description"] = pmeta.get("description", "")
            props[pname] = schema
            if pmeta.get("required"):
                required.append(pname)
        result.append({
            "type": "function",
            "function": {
                "name": d["tool_name"],
                "description": d.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        })
    return result


_OPENAI_TOOLS = _to_openai_tools(TOOL_DEFINITIONS)


def _coerce_args(arguments: dict) -> dict:
    """Coerce LLM-serialized list strings into actual Python lists.

    vLLM sometimes returns list arguments as a Python repr string
    e.g. "['CUST1', 'CUST2']" instead of the JSON array ["CUST1", "CUST2"].
    """
    coerced = {}
    for k, v in arguments.items():
        if isinstance(v, str) and v.startswith("["):
            try:
                coerced[k] = json.loads(v)
            except json.JSONDecodeError:
                try:
                    coerced[k] = ast.literal_eval(v)
                except (ValueError, SyntaxError):
                    coerced[k] = v
        else:
            coerced[k] = v
    return coerced


class AgentRunner:
    def __init__(self) -> None:
        self._client = OpenAI(
            base_url=OGX_URL.rstrip("/") + "/v1",
            api_key="dummy",  # OGX does not require a real key
        )
        self._sessions: dict[str, list[dict]] = {}
        self._real_ids: dict[str, list[str]] = {}  # last list_customers result per session
        log.info("AgentRunner initialised — OGX: %s  model: %s", OGX_URL, OGX_MODEL_ID)

    # ── Session management ────────────────────────────────────────────────────

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = []
        self._real_ids[session_id] = []
        log.debug("Session created: %s", session_id)
        return session_id

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._real_ids.pop(session_id, None)

    # ── Agentic turn ──────────────────────────────────────────────────────────

    async def stream_turn(
        self,
        session_id: str,
        user_message: str,
    ) -> AsyncGenerator[dict, None]:
        history = self._sessions.get(session_id)
        if history is None:
            raise ValueError(f"Unknown session: {session_id}")

        history.append({"role": "user", "content": user_message})

        for _iteration in range(_MAX_TOOL_ITERS):
            messages = [_SYSTEM_MESSAGE] + history
            try:
                tool_calls, assistant_text = await self._call_llm(messages)
            except BadRequestError as exc:
                if "single tool-calls" in str(exc) or "parallel" in str(exc).lower():
                    log.warning("Model attempted parallel tool calls — stopping turn")
                    yield {"type": "error", "content": "The model tried to call multiple tools at once, which is not supported. Please rephrase as a single-step question (e.g. 'show churn risk' then separately 'check fraud')."}
                    return
                raise

            if assistant_text:
                yield {"type": "text", "content": assistant_text}

            if not tool_calls:
                if assistant_text:
                    history.append({"role": "assistant", "content": assistant_text})
                break

            # Record assistant turn — only the first tool call (model supports one at a time)
            history.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": [
                    {
                        "id": c["call_id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for c in tool_calls[:1]
                ],
            })

            # Llama 3.1 8B only supports single tool calls — take the first one only
            for call in tool_calls[:1]:
                fixed_args = self._fix_customer_ids(session_id, call["name"], call["arguments"])
                result = await self._execute_tool(call["name"], fixed_args)
                # Cache real IDs returned by list_customers
                if call["name"] == "list_customers" and isinstance(result, dict) and "customer_ids" in result:
                    self._real_ids[session_id] = result["customer_ids"]
                yield {"type": "tool_call", "name": call["name"], "arguments": fixed_args, "result": result}

                history.append({
                    "role": "tool",
                    "tool_call_id": call["call_id"],
                    "content": json.dumps(result),
                })
        else:
            log.warning("Max tool iterations (%d) reached for session %s", _MAX_TOOL_ITERS, session_id)
            yield {"type": "error", "content": "Reached maximum tool iterations."}

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _call_llm(self, messages: list[dict]) -> tuple[list[dict], str]:
        """Stream one inference call; return (tool_calls, text)."""

        def _sync_call() -> tuple[list[dict], str]:
            stream = self._client.chat.completions.create(
                model=OGX_MODEL_ID,
                messages=messages,
                tools=_OPENAI_TOOLS,
                tool_choice="auto",
                stream=True,
            )

            text = ""
            # Accumulate streamed tool call deltas by index
            tc_acc: dict[int, dict] = {}

            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue
                if delta.content:
                    text += delta.content
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tc_acc:
                            tc_acc[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc_delta.id:
                            tc_acc[idx]["id"] += tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tc_acc[idx]["name"] += tc_delta.function.name
                            if tc_delta.function.arguments:
                                tc_acc[idx]["arguments"] += tc_delta.function.arguments

            tool_calls = []
            for v in tc_acc.values():
                try:
                    arguments = json.loads(v["arguments"]) if v["arguments"] else {}
                except json.JSONDecodeError:
                    arguments = {}
                tool_calls.append({
                    "call_id": v["id"],
                    "name": v["name"],
                    "arguments": arguments,
                })

            return tool_calls, text

        try:
            return await asyncio.to_thread(_sync_call)
        except BadRequestError as exc:
            msg = str(exc)
            if "single tool-calls" in msg or "parallel" in msg.lower():
                log.warning("vLLM rejected request (parallel tool calls attempted by model): %s", exc)
                # Return a sentinel so stream_turn yields an error and stops gracefully
                raise
            raise

    def _fix_customer_ids(self, session_id: str, tool_name: str, arguments: dict) -> dict:
        """Guard prediction tools against hallucinated customer IDs.

        fetch_* tools accept explicit IDs from the user, so they are excluded —
        replacing their customer_ids with the session cache would override a
        specific lookup the analyst just asked for.
        """
        _PREDICT_TOOLS = {"predict_churn", "predict_fraud", "predict_qoe"}
        if tool_name not in _PREDICT_TOOLS:
            return arguments
        real_ids = self._real_ids.get(session_id, [])
        log.info("_fix_customer_ids: tool=%s session=%s real_ids_count=%d llm_args=%s",
                 tool_name, session_id, len(real_ids), arguments)
        if not real_ids:
            log.warning("No real IDs cached for session %s — using LLM-provided IDs", session_id)
            return arguments
        log.info("Substituting LLM customer_ids with %d real IDs for %s", len(real_ids), tool_name)
        return {**arguments, "customer_ids": real_ids[:10]}

    async def _execute_tool(self, name: str, arguments: dict) -> Any:
        fn = TOOL_MAP.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return await asyncio.to_thread(fn, **_coerce_args(arguments))
        except Exception as exc:
            log.exception("Tool %s raised: %s", name, exc)
            return {"error": str(exc)}
