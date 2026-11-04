"""Prediction-tools data-fetch MCP consumer contract tests."""

from __future__ import annotations

from types import TracebackType
from typing import ClassVar

import fastmcp
import pytest
from data_fetch_tool import FetchCustomerDataInput, FetchCustomerDataOutput
from prediction_tools.main import _create_data_fetch_client

from common.errors import ErrorCode, ServiceError


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeCallResult:
    def __init__(
        self,
        *,
        is_error: bool,
        structured_content: dict[str, object] | None,
    ) -> None:
        self.is_error = is_error
        self.structured_content = structured_content


class FakeMcpClient:
    """Small FastMCP client substitute that records the consumer wire call."""

    instances: ClassVar[list[FakeMcpClient]] = []
    tool_present: ClassVar[bool] = True
    call_result: ClassVar[FakeCallResult] = FakeCallResult(is_error=False, structured_content=None)

    def __init__(self, endpoint: str, *, timeout: float) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self.list_calls = 0
        self.called_name: str | None = None
        self.called_arguments: dict[str, object] | None = None
        self.raise_on_error: bool | None = None
        self.instances.append(self)

    async def __aenter__(self) -> FakeMcpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc, traceback
        return False

    async def list_tools(self) -> list[FakeTool]:
        self.list_calls += 1
        if self.tool_present:
            return [FakeTool("fetch_customer_data")]
        return [FakeTool("another_tool")]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        raise_on_error: bool,
    ) -> FakeCallResult:
        self.called_name = name
        self.called_arguments = arguments
        self.raise_on_error = raise_on_error
        return self.call_result


def _request() -> FetchCustomerDataInput:
    return FetchCustomerDataInput(
        entity="customers",
        ids=["customer-1", "customer-2"],
        field_aliases=["churn_features"],
        limit=2,
    )


@pytest.mark.asyncio
async def test_client_uses_only_the_allowlisted_fetch_tool_and_typed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMcpClient.instances = []
    FakeMcpClient.tool_present = True
    expected = FetchCustomerDataOutput.model_validate(
        {
            "records": [
                {
                    "entity": "customers",
                    "record_id": "customer-1",
                    "attributes": {
                        "churn_features": {
                            "PaymentStatus": "Paid",
                            "Balance": 24.5,
                        }
                    },
                }
            ],
            "next_cursor": None,
        }
    )
    FakeMcpClient.call_result = FakeCallResult(
        is_error=False,
        structured_content=expected.to_wire(),
    )
    monkeypatch.setattr(fastmcp, "Client", FakeMcpClient)
    client = _create_data_fetch_client("http://data-fetch.internal/mcp")
    request = _request()

    first = await client.fetch_customer_data(request)
    second = await client.fetch_customer_data(request)

    assert first == expected
    assert second == expected
    assert len(FakeMcpClient.instances) == 2
    for instance in FakeMcpClient.instances:
        assert instance.endpoint == "http://data-fetch.internal/mcp"
        assert instance.timeout == 30.0
        assert instance.list_calls == 1
        assert instance.called_name == "fetch_customer_data"
        assert instance.raise_on_error is False
        assert instance.called_arguments is not None
        assert set(instance.called_arguments) <= {
            "entity",
            "ids",
            "filters",
            "field_aliases",
            "limit",
            "sort",
            "cursor",
        }
        assert instance.called_arguments["entity"] == "customers"
        assert instance.called_arguments["ids"] == ["customer-1", "customer-2"]
        assert instance.called_arguments["field_aliases"] == ["churn_features"]


@pytest.mark.asyncio
async def test_database_unavailable_is_mapped_to_typed_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMcpClient.instances = []
    FakeMcpClient.tool_present = True
    FakeMcpClient.call_result = FakeCallResult(
        is_error=True,
        structured_content={"code": "DATABASE_UNAVAILABLE"},
    )
    monkeypatch.setattr(fastmcp, "Client", FakeMcpClient)
    client = _create_data_fetch_client("http://data-fetch.internal/mcp")

    with pytest.raises(ServiceError) as error:
        await client.fetch_customer_data(_request())

    assert error.value.code == ErrorCode.MODEL_UNAVAILABLE


@pytest.mark.asyncio
async def test_missing_upstream_tool_is_a_typed_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeMcpClient.instances = []
    FakeMcpClient.tool_present = False
    FakeMcpClient.call_result = FakeCallResult(is_error=False, structured_content={})
    monkeypatch.setattr(fastmcp, "Client", FakeMcpClient)
    client = _create_data_fetch_client("http://data-fetch.internal/mcp")

    with pytest.raises(ServiceError) as error:
        await client.fetch_customer_data(_request())

    assert error.value.code == ErrorCode.MODEL_UNAVAILABLE
    assert FakeMcpClient.instances[0].called_name is None
