"""Synchronous OGX Responses adapter with durable logical-session history."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from api.db.session import ApiSettings
from common.errors import ErrorCode, UpstreamError
from common.http import HttpClient

OGX_DEADLINE_SECONDS = 110.0
OGX_SESSION_METADATA_KEY = "telco_session_id"
OGX_CHAT_INSTRUCTIONS = (
    "You are the Telco Revenue Assurance analyst assistant. For questions about customer, "
    "billing, transaction, churn, fraud, or QoE data, use the available tools and base factual "
    "answers only on their results. Use prediction tools for new model scores and the data-fetch "
    "tool for existing records. If the tools cannot answer, say so; do not invent customer "
    "records, scores, or results. For general questions unrelated to this data, answer normally."
)


class OgxRequestRejected(UpstreamError):
    """OGX returned an explicit status that rejects the request before acceptance."""


@dataclass(frozen=True, slots=True)
class OgxReply:
    response_id: str
    conversation_id: str | None
    text: str


class OgxResponsesClient:
    """Send one bounded, non-streaming request to the configured OGX endpoint."""

    def __init__(
        self,
        settings: ApiSettings,
        http_client: HttpClient | None = None,
    ) -> None:
        self._url = f"{str(settings.ogx_base_url).rstrip('/')}/v1/responses"
        self._model_id = settings.ogx_model_id
        self._tools = [
            {
                "type": "mcp",
                "server_label": "prediction-tools",
                "server_url": str(settings.prediction_mcp_url),
            },
            {
                "type": "mcp",
                "server_label": "data-fetch-tool",
                "server_url": str(settings.data_fetch_mcp_url),
            },
        ]
        self._http = http_client or HttpClient(timeout_seconds=OGX_DEADLINE_SECONDS)
        self._owns_http = http_client is None

    async def create_response(
        self, session_id: UUID, message: str, *, previous_response_id: str | None = None
    ) -> OgxReply:
        payload: dict[str, object] = {
            "model": self._model_id,
            "input": message,
            "instructions": OGX_CHAT_INSTRUCTIONS,
            "metadata": {OGX_SESSION_METADATA_KEY: str(session_id)},
            "store": True,
            "stream": False,
            "tools": self._tools,
            "tool_choice": "auto",
        }
        if previous_response_id is not None:
            payload["previous_response_id"] = previous_response_id
        try:
            response = await self._http.request(
                "POST",
                self._url,
                json=payload,
                retryable=False,
                timeout_seconds=OGX_DEADLINE_SECONDS,
            )
        except Exception as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "The analyst assistant is temporarily unavailable.",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            error_type = (
                OgxRequestRejected
                if response.status_code in {400, 401, 403, 429}
                else UpstreamError
            )
            raise error_type(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "The analyst assistant could not complete the request.",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        try:
            body = response.json()
            return _parse_reply(body, str(session_id), previous_response_id)
        except (TypeError, ValueError, KeyError) as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "The analyst assistant returned an invalid response.",
                retryable=False,
            ) from exc

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()


def _parse_reply(
    body: object, expected_session: str, expected_previous_response_id: str | None
) -> OgxReply:
    if not isinstance(body, Mapping):
        raise ValueError("OGX response must be an object.")
    response_id = body.get("id")
    response_object = body.get("object", "response")
    status = body.get("status")
    created_at = body.get("created_at")
    model = body.get("model")
    conversation_id = body.get("conversation")
    metadata = body.get("metadata")
    output = body.get("output")
    if not isinstance(response_id, str) or not response_id:
        raise ValueError("OGX response id is missing.")
    if response_object != "response":
        raise ValueError("OGX response object discriminator is invalid.")
    if status != "completed":
        raise ValueError("OGX response is not completed.")
    if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
        raise ValueError("OGX response creation timestamp is invalid.")
    if not isinstance(model, str):
        raise ValueError("OGX response model is missing.")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise ValueError("OGX response conversation is invalid.")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get(OGX_SESSION_METADATA_KEY) != expected_session
    ):
        raise ValueError("OGX response session metadata does not match.")
    if body.get("previous_response_id") != expected_previous_response_id:
        raise ValueError("OGX response history does not match.")
    if body.get("store") is not True:
        raise ValueError("OGX response was not stored.")
    if not isinstance(output, list) or not output:
        raise ValueError("OGX response output is missing.")

    text_blocks: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if not isinstance(item.get("id"), str):
            raise ValueError("OGX assistant message id is missing.")
        if item.get("role") != "assistant" or item.get("status") != "completed":
            raise ValueError("OGX assistant message is not completed.")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            raise ValueError("OGX assistant message content is missing.")
        for block in content:
            if (
                not isinstance(block, Mapping)
                or block.get("type") != "output_text"
                or not isinstance(block.get("text"), str)
            ):
                raise ValueError("OGX assistant message contains invalid output text.")
            text_blocks.append(block["text"])
    text = "\n".join(text_blocks).strip()
    if not text:
        raise ValueError("OGX returned no completed assistant text.")
    return OgxReply(response_id=response_id, conversation_id=conversation_id, text=text)
