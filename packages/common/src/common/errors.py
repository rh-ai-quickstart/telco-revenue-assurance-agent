"""Typed application errors and protocol envelopes from the frozen error contract."""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from uuid import UUID

from pydantic import Field

from common.models import CommonModel


class ErrorCode(StrEnum):
    """Stable error codes shared across REST, MCP, OGX, database, and KServe boundaries."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_INPUT = "INVALID_INPUT"
    AUTHENTICATION_OR_AUTHORIZATION = "AUTHENTICATION_OR_AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    SUBJECT_NOT_FOUND = "SUBJECT_NOT_FOUND"
    INVALID_MODEL_ID = "INVALID_MODEL_ID"
    INVALID_MODEL_NAME = "INVALID_MODEL_NAME"
    INVALID_TENSOR = "INVALID_TENSOR"
    INVALID_ENTITY = "INVALID_ENTITY"
    INVALID_FIELD = "INVALID_FIELD"
    INVALID_FILTER = "INVALID_FILTER"
    RESULT_LIMIT_EXCEEDED = "RESULT_LIMIT_EXCEEDED"
    RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
    RUN_NOT_COMPLETE = "RUN_NOT_COMPLETE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    FEATURE_SCHEMA_MISMATCH = "FEATURE_SCHEMA_MISMATCH"
    MODEL_NOT_READY = "MODEL_NOT_READY"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    PREPROCESSING_ARTIFACT_MISSING = "PREPROCESSING_ARTIFACT_MISSING"
    STORAGE_ERROR = "STORAGE_ERROR"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(CommonModel):
    """Safe field-level detail used in a REST error envelope."""

    field: str | None = None
    reason: str
    limit: int | None = None
    current_run_id: UUID | None = None


class PublicError(CommonModel):
    """The public REST error body required by the contract."""

    code: ErrorCode
    message: str
    request_id: str
    retryable: bool
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorEnvelope(CommonModel):
    """The outer REST error object."""

    error: PublicError


class MCPErrorPayload(CommonModel):
    """The structured content for a failed MCP tool result."""

    code: ErrorCode
    message: str
    request_id: str
    retryable: bool
    details: dict[str, object] = Field(default_factory=dict)


class ServiceError(Exception):
    """Safe typed exception that can be mapped into REST and MCP error envelopes."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
        rest_details: Sequence[ErrorDetail] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details) if details is not None else {}
        self.rest_details = list(rest_details)

    def to_rest_envelope(self, request_id: str) -> ErrorEnvelope:
        """Build the contract-compliant REST error envelope for this failure."""

        return ErrorEnvelope(
            error=PublicError(
                code=self.code,
                message=self.message,
                request_id=request_id,
                retryable=self.retryable,
                details=self.rest_details,
            )
        )

    def to_mcp_payload(self, request_id: str) -> MCPErrorPayload:
        """Build the structured MCP error payload for this failure."""

        return MCPErrorPayload(
            code=self.code,
            message=self.message,
            request_id=request_id,
            retryable=self.retryable,
            details=self.details,
        )


class RequestError(ServiceError):
    """A safe input or request failure raised before calling a dependency."""


class UpstreamError(ServiceError):
    """A bounded dependency failure mapped to a stable contract error code."""


class DatabaseError(UpstreamError):
    """A database failure mapped without exposing DSNs, SQL, or driver details."""


__all__ = [
    "DatabaseError",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "MCPErrorPayload",
    "PublicError",
    "RequestError",
    "ServiceError",
    "UpstreamError",
]
