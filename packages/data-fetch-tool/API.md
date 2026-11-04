# Data-fetch tool API

The package exposes one MCP tool named `fetch_customer_data`. It accepts the fields and bounds in
`contracts/data-fetch-mcp.json` and returns `FetchCustomerDataOutput` with `records` and
`next_cursor`. Calls are read-only, use a per-call async database session, and accept only the five
contract entities and their per-entity field aliases.

Downstream Python components can depend on the protocol and fake without depending on SQLAlchemy
implementation details:

```python
from data_fetch_tool import DataFetchClient, FetchCustomerDataInput, FetchCustomerDataOutput
from data_fetch_tool.fake import InMemoryDataFetchClient

client: DataFetchClient = InMemoryDataFetchClient(records=[])
request = FetchCustomerDataInput(entity="customers", ids=["customer-00421"], limit=25)
response: FetchCustomerDataOutput = await client.fetch_customer_data(request)
```

The concrete provider is `DatabaseDataFetchClient` in `data_fetch_tool.repository`. MCP adapters
should call the protocol method rather than import its repository internals. Use the fake for
consumer tests; its method signature and public result models are the same as the database client.
