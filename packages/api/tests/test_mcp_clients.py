from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import pytest
from api.db.session import ApiSettings
from api.services import mcp_clients
from api.services.mcp_clients import McpClients, StreamableHttpMcpClient
from data_fetch_tool import FetchCustomerDataInput

from common.errors import ErrorCode, UpstreamError


@dataclass
class FakeToolResult:
    is_error: bool
    structured_content: object


class FakeFastMcpClient:
    def __init__(self, endpoint: str, timeout: float, factory: FakeClientFactory) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._factory = factory

    async def __aenter__(self) -> FakeFastMcpClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
        **kwargs: object,
    ) -> FakeToolResult:
        self._factory.calls.append((tool_name, arguments, kwargs))
        return self._factory.results.pop(0)


class FakeClientFactory:
    def __init__(self, results: list[FakeToolResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, object], dict[str, object]]] = []
        self.instances: list[FakeFastMcpClient] = []

    def __call__(self, endpoint: str, *, timeout: float) -> FakeFastMcpClient:
        client = FakeFastMcpClient(endpoint, timeout, self)
        self.instances.append(client)
        return client


def _settings() -> ApiSettings:
    return ApiSettings(
        database_url="postgresql://api@db.example.test/telco",
        ogx_base_url="http://ogx.example.test:8321",
        ogx_model_id="llama-3.1-8b-instruct",
        prediction_mcp_url="http://prediction.example.test:8000/mcp",
        data_fetch_mcp_url="http://fetch.example.test:8000/mcp",
        scoring_batch_size=25,
    )


def _install_client_factory(monkeypatch: pytest.MonkeyPatch, factory: FakeClientFactory) -> None:
    monkeypatch.setattr(mcp_clients, "Client", cast(Any, factory))


@pytest.mark.asyncio
async def test_streamable_http_adapter_returns_only_structured_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeClientFactory([FakeToolResult(False, {"predictions": [{"customer_id": "c-1"}]})])
    _install_client_factory(monkeypatch, factory)
    client = StreamableHttpMcpClient(_settings().prediction_mcp_url)

    result = await client.call("predict_churn", {"customer_ids": ["c-1"]})

    assert result == {"predictions": [{"customer_id": "c-1"}]}
    assert factory.calls[0][0] == "predict_churn"
    assert factory.calls[0][1] == {"customer_ids": ["c-1"]}
    assert factory.calls[0][2]["raise_on_error"] is False
    assert factory.instances[0].endpoint == "http://prediction.example.test:8000/mcp"
    assert factory.instances[0].timeout == mcp_clients.MCP_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_data_fetch_result_is_validated_against_public_consumer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeClientFactory(
        [
            FakeToolResult(
                False,
                {
                    "records": [
                        {
                            "entity": "customers",
                            "record_id": "customer-1",
                            "attributes": {"customer_id": "customer-1"},
                        }
                    ],
                    "next_cursor": None,
                },
            )
        ]
    )
    _install_client_factory(monkeypatch, factory)
    clients = McpClients.from_settings(_settings())
    request = FetchCustomerDataInput(
        entity="customers",
        ids=["customer-1"],
        field_aliases=["customer_id"],
        limit=10,
    )

    output = await clients.fetch_customer_data(request)

    assert output.records[0].record_id == "customer-1"
    assert output.records[0].attributes["customer_id"] == "customer-1"
    assert factory.calls[0][0] == "fetch_customer_data"
    assert factory.calls[0][1] == {
        "entity": "customers",
        "ids": ["customer-1"],
        "field_aliases": ["customer_id"],
        "limit": 10,
    }
    assert factory.instances[0].endpoint == "http://fetch.example.test:8000/mcp"


@pytest.mark.asyncio
async def test_retryable_typed_tool_failure_retries_once_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeClientFactory(
        [
            FakeToolResult(
                True,
                {"code": "DATABASE_UNAVAILABLE", "retryable": True},
            ),
            FakeToolResult(False, {"predictions": []}),
        ]
    )
    _install_client_factory(monkeypatch, factory)

    async def no_delay(seconds: float) -> None:
        assert seconds == mcp_clients.MCP_RETRY_DELAY_SECONDS

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    clients = McpClients.from_settings(_settings())
    result = await clients.predict_churn(["customer-1"])

    assert result == {"predictions": []}
    assert len(factory.calls) == 2
    assert all(call[0] == "predict_churn" for call in factory.calls)


@pytest.mark.asyncio
async def test_nonretryable_typed_tool_failure_maps_to_safe_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = FakeClientFactory(
        [
            FakeToolResult(
                True,
                {"code": "INVALID_ENTITY", "retryable": True, "message": "private detail"},
            )
        ]
    )
    _install_client_factory(monkeypatch, factory)
    clients = McpClients.from_settings(_settings())

    with pytest.raises(UpstreamError) as caught:
        await clients.predict_churn(["customer-1"])

    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert caught.value.retryable is False
    assert "private detail" not in str(caught.value)
    assert len(factory.calls) == 1
