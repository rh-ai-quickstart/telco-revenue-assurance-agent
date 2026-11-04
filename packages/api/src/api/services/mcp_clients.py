"""Deadline-bound Streamable HTTP clients for the prediction and data-fetch MCPs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol, cast

from data_fetch_tool import FetchCustomerDataInput, FetchCustomerDataOutput
from fastmcp import Client
from pydantic import AnyHttpUrl

from api.db.session import ApiSettings
from common.errors import ErrorCode, UpstreamError

MCP_TIMEOUT_SECONDS = 20.0
MCP_RETRY_DELAY_SECONDS = 0.5


class _ToolCaller(Protocol):
    async def call(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]: ...


class _ToolFailure(Exception):
    def __init__(self, code: str | None, retryable: bool) -> None:
        super().__init__(code or "MCP tool failed.")
        self.code = code
        self.retryable = retryable


class StreamableHttpMcpClient:
    """Call one MCP service through the FastMCP Streamable HTTP boundary."""

    def __init__(self, endpoint: AnyHttpUrl, timeout_seconds: float = MCP_TIMEOUT_SECONDS) -> None:
        self._endpoint = str(endpoint)
        self._timeout_seconds = timeout_seconds

    async def call(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with Client(self._endpoint, timeout=self._timeout_seconds) as client:
                    result = await client.call_tool(
                        tool_name,
                        dict(arguments),
                        timeout=self._timeout_seconds,
                        raise_on_error=False,
                    )
        except TimeoutError as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "A required MCP service exceeded its request deadline.",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "A required MCP service is temporarily unavailable.",
                retryable=True,
            ) from exc

        if result.is_error:
            content = result.structured_content
            if isinstance(content, Mapping):
                raw_code = content.get("code")
                raw_retryable = content.get("retryable")
                raise _ToolFailure(
                    raw_code if isinstance(raw_code, str) else None,
                    raw_retryable is True,
                )
            raise _ToolFailure(None, False)

        structured = result.structured_content
        if not isinstance(structured, Mapping):
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "An MCP service returned an invalid tool result.",
                retryable=False,
            )
        return cast(dict[str, object], dict(structured))


class McpClients:
    """Typed facade used by scoring services for the three prediction tools and data fetch."""

    def __init__(
        self,
        prediction_client: _ToolCaller,
        data_fetch_client: _ToolCaller,
    ) -> None:
        self._prediction = prediction_client
        self._data_fetch = data_fetch_client

    @classmethod
    def from_settings(cls, settings: ApiSettings) -> McpClients:
        return cls(
            prediction_client=StreamableHttpMcpClient(settings.prediction_mcp_url),
            data_fetch_client=StreamableHttpMcpClient(settings.data_fetch_mcp_url),
        )

    async def predict_churn(self, customer_ids: list[str]) -> dict[str, object]:
        return await self._call_prediction("predict_churn", {"customer_ids": customer_ids})

    async def predict_fraud(self, transaction_ids: list[str]) -> dict[str, object]:
        return await self._call_prediction("predict_fraud", {"transaction_ids": transaction_ids})

    async def predict_qoe(self, customer_ids: list[str]) -> dict[str, object]:
        return await self._call_prediction("predict_qoe", {"customer_ids": customer_ids})

    async def fetch_customer_data(self, request: FetchCustomerDataInput) -> FetchCustomerDataOutput:
        payload = request.model_dump(mode="json", exclude_none=True)
        result = await self._call_with_one_retry(
            self._data_fetch,
            "fetch_customer_data",
            cast(dict[str, object], payload),
        )
        try:
            return FetchCustomerDataOutput.model_validate(result)
        except ValueError as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "The data-fetch service returned an invalid result.",
                retryable=False,
            ) from exc

    async def _call_prediction(
        self, tool_name: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        return await self._call_with_one_retry(self._prediction, tool_name, arguments)

    @staticmethod
    async def _call_with_one_retry(
        client: _ToolCaller,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        for attempt in range(2):
            try:
                return await client.call(tool_name, arguments)
            except UpstreamError as exc:
                if attempt == 0 and exc.retryable:
                    await asyncio.sleep(MCP_RETRY_DELAY_SECONDS)
                    continue
                raise
            except _ToolFailure as exc:
                retryable_code = exc.code in {
                    "DATABASE_UNAVAILABLE",
                    "MODEL_UNAVAILABLE",
                    "UPSTREAM_TIMEOUT",
                }
                if attempt == 0 and exc.retryable and retryable_code:
                    await asyncio.sleep(MCP_RETRY_DELAY_SECONDS)
                    continue
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    "A required MCP tool did not complete the request.",
                    retryable=exc.retryable and retryable_code,
                ) from exc
        raise AssertionError("The bounded MCP retry loop did not return or raise.")


def create_mcp_clients(settings: ApiSettings) -> McpClients:
    return McpClients.from_settings(settings)
