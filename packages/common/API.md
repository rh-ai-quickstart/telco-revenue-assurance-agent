# Shared package API

Private by default: if a symbol is not listed here, component packages must treat it as an implementation detail.

## `common.settings`

- `class BaseSettings(pydantic_settings.BaseSettings)` — Validated, environment-backed settings base. Use it for each service settings model so dotenv files are never loaded implicitly.

  ```python
  from common.settings import BaseSettings

  class ServiceSettings(BaseSettings):
      database_url: str
  ```

## `common.models`

- `class CommonModel(pydantic.BaseModel)` — Strict model base with alias serialization, enum values, ISO-8601 datetimes, and explicit nulls. Use it for data crossing package or protocol boundaries.
- `CommonModel.to_wire(self) -> dict[str, object]` — Return the JSON-compatible representation. Use it to produce a contract payload.

  ```python
  from datetime import UTC, datetime
  from common.models import CommonModel

  class Event(CommonModel):
      created_at: datetime

  wire_data = Event(created_at=datetime.now(UTC)).to_wire()
  ```

## `common.errors`

- `class ErrorCode(StrEnum)` — Stable error codes from `contracts/errors.md`. Use an enum value at every protocol boundary.
- `class ErrorDetail(CommonModel): field: str | None = None; reason: str; limit: int | None = None; current_run_id: UUID | None = None` — Safe REST field detail. Use it when a public error can identify a field or bounded limit.
- `class PublicError(CommonModel): code: ErrorCode; message: str; request_id: str; retryable: bool; details: list[ErrorDetail] = []` — REST error body. Use it inside `ErrorEnvelope`.
- `class ErrorEnvelope(CommonModel): error: PublicError` — Outer REST error object. Use it for non-2xx API responses.
- `class MCPErrorPayload(CommonModel): code: ErrorCode; message: str; request_id: str; retryable: bool; details: dict[str, object] = {}` — Structured content for a failed MCP tool result. Use it with the MCP result error flag and repeat its safe message in the text content.
- `ServiceError(code: ErrorCode, message: str, *, retryable: bool = False, details: Mapping[str, object] | None = None, rest_details: Sequence[ErrorDetail] = ()) -> None` — Typed safe exception for protocol adapters. Use it to carry a stable failure code and bounded details.
- `ServiceError.to_rest_envelope(self, request_id: str) -> ErrorEnvelope` — Build the REST error envelope using the supplied request ID.
- `ServiceError.to_mcp_payload(self, request_id: str) -> MCPErrorPayload` — Build the structured MCP payload using the supplied request ID.
- `class RequestError(ServiceError)` — Request failure category; inherits the `ServiceError` constructor. Use it for invalid input detected before a dependency call.
- `class UpstreamError(ServiceError)` — Dependency failure category; inherits the `ServiceError` constructor. Use it for a classified upstream failure.
- `class DatabaseError(UpstreamError)` — Database failure category; inherits the `ServiceError` constructor. Use it without exposing SQL, DSNs, or driver details.

  ```python
  from common.errors import ErrorCode, ServiceError

  error = ServiceError(ErrorCode.MODEL_UNAVAILABLE, "The model is temporarily unavailable.", retryable=True)
  envelope = error.to_rest_envelope(request_id="req_123")
  ```

## `common.http`

- `RetryPolicy(max_retries: int = 0, retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504}), base_delay_seconds: float = 1.0, max_delay_seconds: float = 8.0)` — Immutable retry limits for one client. Use the attempt cap and delays specified by the consuming contract.
- `HttpClient(*, timeout_seconds: float = 10.0, retry_policy: RetryPolicy | None = None, client: httpx.AsyncClient | None = None) -> None` — Bounded async HTTP client. Use it for HTTP integrations that need the shared timeout and retry behavior.
- `HttpClient.request(self, method: str, url: str | httpx.URL, *, retryable: bool = False, timeout_seconds: float | None = None, headers: Mapping[str, str] | None = None, params: Mapping[str, str | int | float | bool | None] | None = None, json: object | None = None, content: bytes | str | None = None) -> httpx.Response` — Send one request and return its response. Set `retryable=True` only when the operation is safe to repeat with its idempotency key or request ID.
- `HttpClient.aclose(self) -> None` — Close the client if this instance created it. Use this during application shutdown.

  ```python
  client = HttpClient(retry_policy=RetryPolicy(max_retries=1))
  response = await client.request("GET", endpoint, retryable=True)
  await client.aclose()
  ```

## `common.logging`

- `class JsonLogFormatter(logging.Formatter)` — One-line JSON formatter with timestamp, level, logger, message, request ID, and safe correlation fields. Use it through `configure_logging`.
- `bind_request_id(request_id: str) -> Iterator[None]` — Context manager binding a request ID to the current async context. Use it around request or tool handling.
- `get_request_id() -> str | None` — Return the request ID bound to the current context, if any. Use it when creating protocol error envelopes.
- `configure_logging(level: int | str = logging.INFO) -> None` — Configure the root logger with the shared JSON format. Call it once during application startup.

  ```python
  import logging
  from common.logging import bind_request_id, configure_logging

  configure_logging()
  logger = logging.getLogger(__name__)
  with bind_request_id("req_123"):
      logger.info("scoring run accepted", extra={"component": "api", "run_id": "run_123"})
  ```
