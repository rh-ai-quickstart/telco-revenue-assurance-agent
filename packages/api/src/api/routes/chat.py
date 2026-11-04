"""Public synchronous chat request/reply route."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Path, Request

from common.errors import ErrorCode, ErrorEnvelope, UpstreamError

from ..services.chat import ChatMessageRequest, ChatMessageResponse, ChatService

CHAT_DEADLINE_SECONDS = 120.0

router = APIRouter(tags=["Chat"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "The request failed validation."},
    401: {"model": ErrorEnvelope, "description": "Authentication is required."},
    403: {"model": ErrorEnvelope, "description": "The caller is not authorized."},
    409: {"model": ErrorEnvelope, "description": "The request ID conflicts with a prior message."},
    500: {"model": ErrorEnvelope, "description": "The request could not be completed."},
    503: {"model": ErrorEnvelope, "description": "A required service is temporarily unavailable."},
}


def _service(request: Request) -> ChatService:
    return cast(ChatService, request.app.state.chat_service)


@router.post(
    "/chat/sessions/{session_id}/messages",
    operation_id="sendChatMessage",
    summary="Send a message to one durable agent session",
    description=(
        "The first message materializes the specified session in OGX PostgreSQL state; "
        "later messages continue only that same session. The backend passes the "
        "session_id to OGX and does not choose a session on the caller's behalf."
    ),
    response_model=ChatMessageResponse,
    responses=_ERROR_RESPONSES,
    openapi_extra={
        "x-side-effects": (
            "Writes conversation state for the path session_id. Reusing request_id with "
            "identical content returns the original response; reusing it with different "
            "content returns IDEMPOTENCY_CONFLICT."
        ),
        "x-error-codes": [
            "VALIDATION_ERROR",
            "AUTHENTICATION_OR_AUTHORIZATION",
            "IDEMPOTENCY_CONFLICT",
            "UPSTREAM_UNAVAILABLE",
            "INTERNAL_ERROR",
        ],
        "x-timeouts-and-retries": (
            "Hard deadline is 120 seconds; there is no latency SLA. A client may retry "
            "UPSTREAM_UNAVAILABLE at most twice with exponential backoff from 1 to 8 "
            "seconds and must reuse request_id."
        ),
        "x-ordering": (
            "Requests for one session are serialized; independent sessions may run concurrently."
        ),
        "x-versioning": "/api/v1; breaking request or response changes require /api/v2.",
    },
)
async def send_chat_message(
    session_id: Annotated[
        UUID,
        Path(description="UUID chosen by the client and retained to resume this conversation."),
    ],
    body: ChatMessageRequest,
    request: Request,
) -> ChatMessageResponse:
    try:
        async with asyncio.timeout(CHAT_DEADLINE_SECONDS):
            return await _service(request).send_message(
                session_id=session_id,
                request_id=body.request_id,
                message=body.message,
            )
    except TimeoutError as exc:
        raise UpstreamError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The chat request exceeded its response deadline.",
            retryable=True,
        ) from exc
