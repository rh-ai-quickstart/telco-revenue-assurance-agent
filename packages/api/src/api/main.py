"""FastAPI application factory and service lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ExceptionHandler

from common.errors import ErrorCode, ErrorEnvelope, ServiceError
from common.logging import bind_request_id, configure_logging

from .db.session import ApiSettings, create_database_engine, create_session_factory
from .routes.chat import router as chat_router
from .routes.metrics import router as metrics_router
from .routes.scoring import router as scoring_router
from .services.chat import ChatService, PostgresSessionLock
from .services.mcp_clients import create_mcp_clients
from .services.ogx_client import OgxResponsesClient
from .services.scoring import ScoringService

logger = logging.getLogger(__name__)
API_PREFIX = "/api/v1"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def create_app(settings_override: ApiSettings | None = None) -> FastAPI:
    """Create an app without reading required deployment settings at import time."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings = settings_override or ApiSettings()
        configure_logging()
        engine = create_database_engine(settings)
        session_factory = create_session_factory(engine)
        mcp_clients = create_mcp_clients(settings)
        ogx_client = OgxResponsesClient(settings)
        scoring_service = ScoringService(
            session_factory,
            engine,
            mcp_clients,
            settings.scoring_batch_size,
        )
        application.state.settings = settings
        application.state.engine = engine
        application.state.session_factory = session_factory
        application.state.scoring_service = scoring_service
        application.state.chat_service = ChatService(
            session_factory,
            ogx_client,
            PostgresSessionLock(engine),
        )

        resume_task = asyncio.create_task(
            _resume_explicitly_accepted_runs(scoring_service),
            name="resume-accepted-scoring-runs",
        )
        try:
            yield
        finally:
            resume_task.cancel()
            await asyncio.gather(resume_task, return_exceptions=True)
            await ogx_client.aclose()
            await engine.dispose()

    application = FastAPI(
        title="Telco Revenue Assurance API",
        version="1.0.0",
        description=(
            "Same-origin REST API for the analyst dashboard. The authenticated OpenShift Route "
            "is the authentication boundary; the backend trusts identity only from the "
            "configured trusted ingress or proxy. Chat is synchronous request/reply and has "
            "no latency service-level objective in v1."
        ),
        lifespan=lifespan,
    )
    application.include_router(scoring_router, prefix=API_PREFIX)
    application.include_router(metrics_router, prefix=API_PREFIX)
    application.include_router(chat_router, prefix=API_PREFIX)
    application.middleware("http")(request_context_middleware)
    application.add_exception_handler(
        ServiceError,
        cast(ExceptionHandler, service_error_handler),
    )
    application.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, validation_error_handler),
    )
    application.add_exception_handler(
        StarletteHTTPException,
        cast(ExceptionHandler, http_error_handler),
    )
    application.add_exception_handler(Exception, internal_error_handler)

    default_openapi = application.openapi

    def contract_openapi() -> dict[str, Any]:
        if application.openapi_schema is not None:
            return application.openapi_schema
        schema = default_openapi()
        schema["openapi"] = "3.1.0"
        schema["servers"] = [
            {
                "url": API_PREFIX,
                "description": "Same-origin API path behind the OAuth-protected Route",
            }
        ]
        schema["security"] = [{"OpenShiftOAuth": []}]
        schema["x-contract-guarantees"] = {
            "side_effects": (
                "GET operations are read-only. POST /scoring-runs queues one explicit run of all "
                "three models and is idempotent by Idempotency-Key. Chat writes only the named "
                "session and deduplicates a repeated request_id within that session."
            ),
            "errors": (
                "Errors use ErrorEnvelope. Operations document VALIDATION_ERROR, "
                "AUTHENTICATION_OR_AUTHORIZATION, NOT_FOUND, RUN_ALREADY_ACTIVE, RUN_NOT_COMPLETE, "
                "IDEMPOTENCY_CONFLICT, UPSTREAM_UNAVAILABLE, and INTERNAL_ERROR where reachable."
            ),
            "timeouts_retries": (
                "Scoring creation has a 5 second request deadline; "
                "reads have a 10 second deadline; "
                "chat has a 120 second hard deadline and no latency SLA. Clients may retry 503 at "
                "most twice with exponential backoff starting at 1 second and capped at 8 seconds, "
                "honoring Retry-After. Do not retry 4xx responses."
            ),
            "ordering": (
                "Runs are listed newest-first with run_id as the stable tie-breaker. Result pages "
                "are ordered by model_type then subject_id. Messages are serialized per session. "
                "GET snapshots have no cross-request consistency guarantee while a run is active."
            ),
            "versioning": (
                "The path prefix is /api/v1. Breaking changes require a new path prefix; additive "
                "response fields are allowed only when consumers ignore unknown fields."
            ),
        }
        components = schema.setdefault("components", {})
        if isinstance(components, dict):
            security_schemes = components.setdefault("securitySchemes", {})
            if isinstance(security_schemes, dict):
                security_schemes["OpenShiftOAuth"] = {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "OAuth2 access token",
                    "description": (
                        "Authentication is enforced by the configured OAuth-protected Route. "
                        "The backend trusts identity only from the configured trusted ingress "
                        "or proxy; it must not trust client-supplied identity headers on a "
                        "direct connection."
                    ),
                }
            schemas = components.get("schemas")
            if isinstance(schemas, dict):
                response_schema = schemas.pop("ScoringRunResponse", None)
                if isinstance(response_schema, dict):
                    response_schema["title"] = "ScoringRun"
                    schemas["ScoringRun"] = response_schema
                error_detail = schemas.get("ErrorDetail")
                if isinstance(error_detail, dict):
                    error_detail["required"] = [
                        "field",
                        "reason",
                        "limit",
                        "current_run_id",
                    ]
                public_error = schemas.get("PublicError")
                if isinstance(public_error, dict):
                    public_error["required"] = [
                        "code",
                        "message",
                        "request_id",
                        "retryable",
                        "details",
                    ]
                    properties = public_error.get("properties")
                    if isinstance(properties, dict):
                        properties["code"] = {
                            "type": "string",
                            "description": (
                                "Stable machine-readable error code from contracts/errors.md."
                            ),
                        }
        paths = schema.get("paths")
        if isinstance(paths, dict):
            normalized_paths: dict[str, object] = {}
            for path, operations in paths.items():
                normalized = path.removeprefix(API_PREFIX)
                normalized_paths[normalized or "/"] = operations
                if isinstance(operations, dict):
                    for operation in operations.values():
                        if isinstance(operation, dict):
                            responses = operation.get("responses")
                            if isinstance(responses, dict):
                                responses.pop("422", None)
            schema["paths"] = normalized_paths
        _replace_schema_refs(schema, "ScoringRunResponse", "ScoringRun")
        _normalize_nullable_schemas(schema)
        _normalize_query_parameter_schemas(schema)
        schema["tags"] = [
            {"name": "Scoring"},
            {"name": "Metrics"},
            {"name": "Chat"},
        ]
        application.openapi_schema = schema
        return schema

    application.openapi = contract_openapi  # type: ignore[method-assign]
    return application


def _replace_schema_refs(value: object, old_name: str, new_name: str) -> None:
    if isinstance(value, dict):
        if value.get("$ref") == f"#/components/schemas/{old_name}":
            value["$ref"] = f"#/components/schemas/{new_name}"
        for nested in value.values():
            _replace_schema_refs(nested, old_name, new_name)
    elif isinstance(value, list):
        for nested in value:
            _replace_schema_refs(nested, old_name, new_name)


def _normalize_nullable_schemas(value: object) -> None:
    if isinstance(value, dict):
        for nested in list(value.values()):
            _normalize_nullable_schemas(nested)
        alternatives = value.get("anyOf")
        if isinstance(alternatives, list) and len(alternatives) == 2:
            non_null = [
                item
                for item in alternatives
                if not (isinstance(item, dict) and item.get("type") == "null")
            ]
            if len(non_null) == 1 and isinstance(non_null[0], dict):
                base = dict(non_null[0])
                schema_type = base.get("type")
                if isinstance(schema_type, str):
                    base["type"] = [schema_type, "null"]
                    base.update({key: item for key, item in value.items() if key != "anyOf"})
                    value.clear()
                    value.update(base)
    elif isinstance(value, list):
        for nested in value:
            _normalize_nullable_schemas(nested)


def _normalize_query_parameter_schemas(schema: dict[str, Any]) -> None:
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if not isinstance(parameter, dict) or parameter.get("in") != "query":
                    continue
                parameter_schema = parameter.get("schema")
                if not isinstance(parameter_schema, dict):
                    continue
                schema_type = parameter_schema.get("type")
                if isinstance(schema_type, list) and "null" in schema_type:
                    non_null_types = [item for item in schema_type if item != "null"]
                    if len(non_null_types) == 1:
                        parameter_schema["type"] = non_null_types[0]


async def _resume_explicitly_accepted_runs(service: ScoringService) -> None:
    try:
        await service.resume_unfinished()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("failed to resume explicitly accepted scoring runs")


async def request_context_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if _REQUEST_ID_PATTERN.fullmatch(supplied) else f"req_{uuid4().hex}"
    request.state.request_id = request_id
    with bind_request_id(request_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else f"req_{uuid4().hex}"


def _status_for(code: ErrorCode) -> int:
    if code == ErrorCode.VALIDATION_ERROR:
        return 400
    if code in {ErrorCode.AUTHENTICATION_OR_AUTHORIZATION}:
        return 401
    if code == ErrorCode.NOT_FOUND:
        return 404
    if code in {
        ErrorCode.RUN_ALREADY_ACTIVE,
        ErrorCode.RUN_NOT_COMPLETE,
        ErrorCode.IDEMPOTENCY_CONFLICT,
    }:
        return 409
    if code in {
        ErrorCode.DATABASE_UNAVAILABLE,
        ErrorCode.MODEL_UNAVAILABLE,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.UPSTREAM_TIMEOUT,
    }:
        return 503
    return 500


def _error_response(request: Request, error: ServiceError) -> JSONResponse:
    status_code = _status_for(error.code)
    envelope: ErrorEnvelope = error.to_rest_envelope(_request_id(request))
    headers = {"Retry-After": "1"} if error.retryable and status_code == 503 else None
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers=headers,
    )


async def service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, ServiceError):
        return _error_response(request, exc)
    return _error_response(
        request,
        ServiceError(ErrorCode.INTERNAL_ERROR, "The request could not be completed."),
    )


async def validation_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    del exc
    error = ServiceError(ErrorCode.VALIDATION_ERROR, "The request failed validation.")
    return _error_response(request, error)


async def http_error_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        return _error_response(
            request,
            ServiceError(ErrorCode.INTERNAL_ERROR, "The request could not be completed."),
        )
    if exc.status_code == 404:
        code = ErrorCode.NOT_FOUND
        message = "The requested resource was not found."
    elif exc.status_code in {401, 403}:
        code = ErrorCode.AUTHENTICATION_OR_AUTHORIZATION
        message = "The caller is not authorized for this operation."
    else:
        code = ErrorCode.VALIDATION_ERROR
        message = "The request failed validation."
    error = ServiceError(code, message)
    response = _error_response(request, error)
    if exc.status_code == 403:
        response.status_code = 403
    return response


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled API request failure",
        extra={"component": "api", "request_id": _request_id(request)},
    )
    del exc
    error = ServiceError(ErrorCode.INTERNAL_ERROR, "The request could not be completed.")
    return _error_response(request, error)


app = create_app()
