"""Contract checks for the frozen data-fetch MCP surface."""

import json
from pathlib import Path
from typing import cast

import pytest
from data_fetch_tool import (
    DataFetchClient,
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
    FetchRecord,
)
from data_fetch_tool.fake import InMemoryDataFetchClient
from data_fetch_tool.main import create_server
from data_fetch_tool.repository import DatabaseDataFetchClient
from fastmcp import Client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MCP_CONTRACT = json.loads(Path("contracts/data-fetch-mcp.json").read_text(encoding="utf-8"))


def _customer(customer_id: str, dollar_weight: float) -> FetchRecord:
    return FetchRecord(
        entity="customers",
        record_id=customer_id,
        attributes={
            "customer_id": customer_id,
            "plan_type": "mobile",
            "dollar_weight": dollar_weight,
            "region_id": "region-west",
        },
    )


def test_real_and_fake_implement_the_consumer_protocol() -> None:
    fake: DataFetchClient = InMemoryDataFetchClient(records=[])
    real: DataFetchClient = DatabaseDataFetchClient(
        cast(async_sessionmaker[AsyncSession], object()),
        max_fetch_rows=10,
    )

    assert isinstance(fake, DataFetchClient)
    assert isinstance(real, DataFetchClient)


@pytest.mark.asyncio
async def test_mcp_exposes_only_frozen_tool_and_returns_bounded_records() -> None:
    expected_tool = _MCP_CONTRACT["x-tool-definition"]["name"]
    records = [_customer("customer-1", 40.5), _customer("customer-2", 92.25)]
    server = create_server(InMemoryDataFetchClient(records), max_fetch_rows=1)

    async with Client(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            expected_tool,
            {
                "entity": "customers",
                "ids": ["customer-1", "customer-2"],
                "field_aliases": ["customer_id", "dollar_weight"],
                "limit": 1,
            },
        )

    assert [tool.name for tool in tools] == [expected_tool]
    assert result.is_error is not True
    assert isinstance(result.structured_content, dict)
    payload = cast(dict[str, object], result.structured_content)
    output = FetchCustomerDataOutput.model_validate(payload)
    assert output.records == [
        FetchRecord(
            entity="customers",
            record_id="customer-1",
            attributes={"customer_id": "customer-1", "dollar_weight": 40.5},
        )
    ]
    assert output.next_cursor is not None


@pytest.mark.asyncio
async def test_omitted_empty_and_explicit_fields_share_the_default_shape() -> None:
    client = InMemoryDataFetchClient([_customer("customer-1", 40.5)])
    base: dict[str, object] = {"entity": "customers", "ids": ["customer-1"], "limit": 5}
    explicit = ["customer_id", "plan_type", "dollar_weight", "region_id"]

    omitted = await client.fetch_customer_data(FetchCustomerDataInput.model_validate(base))
    empty = await client.fetch_customer_data(
        FetchCustomerDataInput.model_validate({**base, "field_aliases": []})
    )
    listed = await client.fetch_customer_data(
        FetchCustomerDataInput.model_validate({**base, "field_aliases": explicit})
    )

    assert omitted.to_wire() == empty.to_wire() == listed.to_wire()
    assert list(omitted.records[0].attributes) == explicit


@pytest.mark.asyncio
async def test_unknown_alias_returns_a_structured_error_without_sql_details() -> None:
    server = create_server(
        InMemoryDataFetchClient([_customer("customer-1", 40.5)]),
        max_fetch_rows=10,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "fetch_customer_data",
            {
                "entity": "customers",
                "ids": ["customer-1"],
                "field_aliases": ["private_column"],
                "limit": 1,
            },
            raise_on_error=False,
        )

    assert result.is_error is True
    assert isinstance(result.structured_content, dict)
    error = cast(dict[str, object], result.structured_content)
    assert error["code"] == "INVALID_FIELD"
    assert isinstance(error["request_id"], str)
    assert "sql" not in str(error).casefold()
