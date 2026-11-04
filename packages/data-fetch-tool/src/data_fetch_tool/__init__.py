"""Read-only data-fetch MCP service."""

from data_fetch_tool.client import DataFetchClient
from data_fetch_tool.schemas import (
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
    FetchRecord,
)

__all__ = [
    "DataFetchClient",
    "FetchCustomerDataInput",
    "FetchCustomerDataOutput",
    "FetchRecord",
]
