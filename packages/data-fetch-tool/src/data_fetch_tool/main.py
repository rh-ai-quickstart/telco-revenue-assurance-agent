"""FastMCP streamable-HTTP entry point and protocol adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated
from uuid import uuid4

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from pydantic import Field, SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.errors import ErrorCode, RequestError, ServiceError
from common.logging import get_request_id
from common.settings import BaseSettings
from data_fetch_tool.client import DataFetchClient
from data_fetch_tool.repository import DatabaseDataFetchClient, create_readonly_engine
from data_fetch_tool.schemas import (
    ENTITY_VALUES,
    FIELD_ALIAS_VALUES,
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
    FilterInput,
    SortInput,
)

logger = logging.getLogger(__name__)

_EntityArgument = Annotated[
    str,
    Field(min_length=1, max_length=32, json_schema_extra={"enum": list(ENTITY_VALUES)}),
]
_FieldAliasArgument = Annotated[
    str,
    Field(min_length=1, max_length=32, json_schema_extra={"enum": list(FIELD_ALIAS_VALUES)}),
]
_StableIdArgument = Annotated[str, Field(min_length=1, max_length=200)]


class ServiceSettings(BaseSettings):
    """Environment-backed service configuration; the DSN comes from a Secret."""

    database_readonly_url: SecretStr
    mcp_host: str = Field(min_length=1)
    mcp_port: int = Field(gt=0, le=65_535)
    max_fetch_rows: int = Field(ge=1, le=100)


def load_service_settings() -> ServiceSettings:
    """Build settings from Pydantic's configured environment sources."""
    return ServiceSettings()


def create_server(client: DataFetchClient, max_fetch_rows: int) -> FastMCP:
    """Create a server with the single frozen read-only MCP tool."""
    if not 1 <= max_fetch_rows <= 100:
        raise ValueError("max_fetch_rows must be between 1 and 100.")
    server = FastMCP(name="Telco Data Fetch", mask_error_details=True)

    @server.tool(
        name="fetch_customer_data",
        description=(
            "Read allowlisted customer, billing-cycle, transaction, QoE sample, or persisted "
            "prediction data by stable IDs and approved filters."
        ),
        output_schema=FetchCustomerDataOutput.model_json_schema(),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def fetch_customer_data(
        *,
        entity: _EntityArgument,
        ids: Annotated[
            list[_StableIdArgument] | None,
            Field(
                min_length=1,
                max_length=100,
                json_schema_extra={"uniqueItems": True},
            ),
        ] = None,
        field_aliases: Annotated[
            list[_FieldAliasArgument] | None,
            Field(max_length=19, json_schema_extra={"uniqueItems": True}),
        ] = None,
        filters: Annotated[list[FilterInput] | None, Field(min_length=1, max_length=10)] = None,
        sort: SortInput | None = None,
        limit: Annotated[int, Field(ge=1, le=100)],
        cursor: Annotated[str | None, Field(max_length=1024)] = None,
    ) -> ToolResult:
        """Return only typed fields from one allowlisted entity."""
        try:
            request = FetchCustomerDataInput(
                entity=entity,
                ids=ids,
                field_aliases=field_aliases,
                filters=filters,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError as exc:
            return _validation_error_result(exc)

        try:
            response = await client.fetch_customer_data(request)
        except ServiceError as exc:
            return _service_error_result(exc)
        except Exception as exc:
            logger.exception("Unexpected data-fetch failure")
            failure = RequestError(
                ErrorCode.DATABASE_UNAVAILABLE,
                "The data store is temporarily unavailable.",
                retryable=False,
            )
            return _service_error_result(failure, cause=exc)

        wire_response = response.to_wire()
        return ToolResult(
            content=json.dumps(wire_response, separators=(",", ":")),
            structured_content=wire_response,
        )

    return server


def _validation_error_result(error: ValidationError) -> ToolResult:
    locations = {str(location) for detail in error.errors() for location in detail.get("loc", ())}
    if "entity" in locations:
        code = ErrorCode.INVALID_ENTITY
        message = "Entity is not supported."
    elif "field_aliases" in locations:
        code = ErrorCode.INVALID_FIELD
        message = "Field aliases are invalid or exceed the configured bound."
    elif locations.intersection({"ids", "filters", "limit", "cursor"}):
        code = ErrorCode.RESULT_LIMIT_EXCEEDED
        message = "A request value exceeds its configured bound."
    else:
        code = ErrorCode.INVALID_FILTER
        message = "The request does not match the supported filter schema."
    return _service_error_result(RequestError(code, message))


def _service_error_result(error: ServiceError, *, cause: Exception | None = None) -> ToolResult:
    if cause is not None:
        logger.debug("Safe data-fetch error returned", extra={"error_code": error.code.value})
    request_id = get_request_id() or f"req_{uuid4().hex}"
    payload = error.to_mcp_payload(request_id).to_wire()
    return ToolResult(
        content=error.message,
        structured_content=payload,
        is_error=True,
    )


async def _serve(settings: ServiceSettings) -> None:
    engine = create_readonly_engine(settings.database_readonly_url)
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    client = DatabaseDataFetchClient(sessions, settings.max_fetch_rows)
    server = create_server(client, settings.max_fetch_rows)
    try:
        await server.run_async(
            transport="http",
            host=settings.mcp_host,
            port=settings.mcp_port,
        )
    finally:
        await engine.dispose()


def main() -> None:
    """Load required settings, then run the MCP streamable-HTTP service."""
    settings = load_service_settings()
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
