"""Consumer-facing protocol for the data-fetch operation."""

from typing import Protocol, runtime_checkable

from data_fetch_tool.schemas import FetchCustomerDataInput, FetchCustomerDataOutput


@runtime_checkable
class DataFetchClient(Protocol):
    """Stable async interface shared by the database adapter and in-memory fake."""

    async def fetch_customer_data(self, request: FetchCustomerDataInput) -> FetchCustomerDataOutput:
        """Return bounded, allowlisted records for one entity."""
