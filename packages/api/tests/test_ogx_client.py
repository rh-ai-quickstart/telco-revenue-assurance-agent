from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from api.db.session import ApiSettings
from api.services.ogx_client import (
    OGX_CHAT_INSTRUCTIONS,
    OGX_DEADLINE_SECONDS,
    OgxReply,
    OgxRequestRejected,
    OgxResponsesClient,
)

from common.errors import UpstreamError
from common.http import HttpClient


def _settings() -> ApiSettings:
    return ApiSettings(
        database_url="postgresql://api@db.example.test/telco",
        ogx_base_url="http://ogx.example.test:8321",
        ogx_model_id="llama-3.1-8b-instruct",
        prediction_mcp_url="http://prediction.example.test:8000/mcp",
        data_fetch_mcp_url="http://fetch.example.test:8000/mcp",
        scoring_batch_size=25,
    )


def _response_body(session_id: UUID, previous_response_id: str | None = None) -> dict[str, object]:
    return {
        "id": "resp_01J8Z2N5M9K7",
        "object": "response",
        "status": "completed",
        "created_at": 1792581000,
        "model": "llama-3.1-8b-instruct",
        "metadata": {"telco_session_id": str(session_id)},
        "previous_response_id": previous_response_id,
        "store": True,
        "output": [
            {"type": "function_call", "name": "fetch_customer_data", "arguments": "{}"},
            {"type": "function_call_output", "output": "internal tool output"},
            {
                "id": "msg_01J8Z2N5M9K8",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "First line."},
                    {"type": "output_text", "text": "Second line."},
                ],
            },
        ],
    }


async def _send_body(session_id: UUID, body: dict[str, object]) -> OgxReply:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=body)

    raw_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OgxResponsesClient(_settings(), HttpClient(client=raw_client))
    try:
        return await client.create_response(session_id, "question")
    finally:
        await raw_client.aclose()


@pytest.mark.asyncio
async def test_responses_request_uses_logical_session_metadata_and_maps_final_text_only() -> None:
    requests: list[httpx.Request] = []
    session_id = uuid4()
    body = _response_body(session_id)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body)

    raw_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    http_client = HttpClient(client=raw_client)
    client = OgxResponsesClient(_settings(), http_client)
    try:
        reply = await client.create_response(session_id, "Which customers need attention?")
    finally:
        await raw_client.aclose()

    assert reply.response_id == body["id"]
    assert reply.conversation_id is None
    assert reply.text == "First line.\nSecond line."
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/responses"
    assert json.loads(request.content) == {
        "model": "llama-3.1-8b-instruct",
        "input": "Which customers need attention?",
        "instructions": OGX_CHAT_INSTRUCTIONS,
        "metadata": {"telco_session_id": str(session_id)},
        "store": True,
        "stream": False,
        "tools": [
            {
                "type": "mcp",
                "server_label": "prediction-tools",
                "server_url": "http://prediction.example.test:8000/mcp",
            },
            {
                "type": "mcp",
                "server_label": "data-fetch-tool",
                "server_url": "http://fetch.example.test:8000/mcp",
            },
        ],
        "tool_choice": "auto",
    }
    assert "conversation" not in json.loads(request.content)
    assert "previous_response_id" not in json.loads(request.content)
    assert "request_id" not in json.loads(request.content)
    assert "idempotency-key" not in request.headers
    timeout = request.extensions["timeout"]
    assert max(timeout.values()) <= OGX_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object", "response.v2"),
        ("status", "in_progress"),
        ("created_at", True),
        ("created_at", -1),
        ("model", 123),
    ],
)
@pytest.mark.asyncio
async def test_invalid_required_response_fields_are_rejected(
    field: str,
    value: object,
) -> None:
    session_id = uuid4()
    body = _response_body(session_id)
    body[field] = value

    with pytest.raises(UpstreamError) as caught:
        await _send_body(session_id, body)

    assert caught.value.retryable is False


@pytest.mark.parametrize("field", ["status", "created_at", "model"])
@pytest.mark.asyncio
async def test_missing_required_response_fields_are_rejected(field: str) -> None:
    session_id = uuid4()
    body = _response_body(session_id)
    del body[field]

    with pytest.raises(UpstreamError) as caught:
        await _send_body(session_id, body)

    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_assistant_message_requires_its_contract_id() -> None:
    session_id = uuid4()
    body = _response_body(session_id)
    output = body["output"]
    assert isinstance(output, list)
    assistant_message = output[-1]
    assert isinstance(assistant_message, dict)
    del assistant_message["id"]

    with pytest.raises(UpstreamError) as caught:
        await _send_body(session_id, body)

    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("status", "rejected", "retryable"),
    [
        (400, True, False),
        (401, True, False),
        (403, True, False),
        (429, True, True),
        (500, False, True),
        (502, False, True),
    ],
)
@pytest.mark.asyncio
async def test_explicit_rejections_are_distinguished_from_ambiguous_server_failures(
    status: int,
    rejected: bool,
    retryable: bool,
) -> None:
    raw_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    )
    client = OgxResponsesClient(_settings(), HttpClient(client=raw_client))
    try:
        with pytest.raises(UpstreamError) as caught:
            await client.create_response(uuid4(), "message")
    finally:
        await raw_client.aclose()

    assert isinstance(caught.value, OgxRequestRejected) is rejected
    assert caught.value.retryable is retryable


@pytest.mark.asyncio
async def test_transport_timeout_is_ambiguous_and_not_retried() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        del request
        call_count += 1
        raise httpx.ReadTimeout("timeout")

    raw_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OgxResponsesClient(_settings(), HttpClient(client=raw_client))
    try:
        with pytest.raises(UpstreamError) as caught:
            await client.create_response(uuid4(), "message")
    finally:
        await raw_client.aclose()

    assert not isinstance(caught.value, OgxRequestRejected)
    assert caught.value.retryable is True
    assert call_count == 1


@pytest.mark.asyncio
async def test_followup_request_passes_previous_response_id_without_conversation() -> None:
    session_id = uuid4()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response_body(session_id, "resp_parent"))

    raw_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OgxResponsesClient(_settings(), HttpClient(client=raw_client))
    try:
        reply = await client.create_response(
            session_id, "followup", previous_response_id="resp_parent"
        )
    finally:
        await raw_client.aclose()
    assert reply.text.splitlines() == ["First line.", "Second line."]
    payload = json.loads(requests[0].content)
    assert payload["previous_response_id"] == "resp_parent"
    assert payload["metadata"] == {"telco_session_id": str(session_id)}
    assert payload["store"] is True
    assert "conversation" not in payload
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("metadata", None),
        ("metadata", {}),
        ("metadata", {"telco_session_id": "another-session"}),
        ("previous_response_id", "unexpected-parent"),
        ("store", False),
    ],
)
@pytest.mark.asyncio
async def test_response_requires_matching_session_history_and_storage(
    field: str, value: object
) -> None:
    session_id = uuid4()
    body = _response_body(session_id)
    body[field] = value
    with pytest.raises(UpstreamError) as rejected:
        await _send_body(session_id, body)
    assert rejected.value.retryable is False


@pytest.mark.asyncio
async def test_response_accepts_default_object_and_null_conversation() -> None:
    session_id = uuid4()
    body = _response_body(session_id)
    body.pop("object")
    body["conversation"] = None
    reply = await _send_body(session_id, body)
    assert reply.conversation_id is None
    assert reply.text.splitlines() == ["First line.", "Second line."]
