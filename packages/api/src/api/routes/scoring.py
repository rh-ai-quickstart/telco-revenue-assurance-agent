"""REST routes and wire models for explicitly requested scoring runs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request, Response
from pydantic import Field

from common.errors import ErrorCode, ErrorEnvelope, ServiceError, UpstreamError
from common.models import CommonModel

from ..db.models import ModelFamily, ScoringRun, ScoringRunStatus
from ..services.scoring import ScoringService

router = APIRouter(tags=["Scoring"])
SCORING_CREATE_DEADLINE_SECONDS = 5.0
API_READ_DEADLINE_SECONDS = 10.0


def _deadline_error(operation: str) -> UpstreamError:
    return UpstreamError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        f"The {operation} exceeded its request deadline.",
        retryable=True,
    )


class ModelProgress(CommonModel):
    status: Literal["queued", "running", "completed", "failed"] = Field(
        description="State of this model's portion of the scoring run."
    )
    processed_rows: int = Field(ge=0, description="Persisted source subjects processed so far.")
    total_rows: int = Field(
        ge=0,
        description="Expected subjects for this model in the configured source snapshot.",
    )


class ScoringRunProgress(CommonModel):
    churn: ModelProgress
    fraud: ModelProgress
    qoe: ModelProgress


class ScoringRunAccepted(CommonModel):
    run_id: UUID = Field(description="Durable identifier for polling and result queries.")
    status: Literal["queued"] = Field(
        description="Newly accepted work is queued, not executed in the request handler."
    )
    created_at: datetime = Field(description="UTC acceptance time.")


class ScoringRunResponse(CommonModel):
    run_id: UUID = Field(description="Durable scoring-run identifier.")
    status: Literal["queued", "running", "completed", "failed"] = Field(
        description="Monotonic overall run state."
    )
    created_at: datetime = Field(description="UTC acceptance time.")
    started_at: datetime | None = Field(
        description="UTC work start time, or null before a worker claims the run."
    )
    completed_at: datetime | None = Field(
        description="UTC completion time, or null while queued or running."
    )
    progress: ScoringRunProgress
    error_code: str | None = Field(
        description="Stable safe error code when the run failed; otherwise null."
    )


class ScoringRunPage(CommonModel):
    items: list[ScoringRunResponse]
    next_cursor: str | None


class PredictionResult(CommonModel):
    prediction_id: UUID = Field(description="Persisted prediction result identifier.")
    run_id: UUID = Field(description="Scoring run that produced this result.")
    model_type: Literal["churn", "fraud", "qoe"] = Field(
        description="Model family that produced the result."
    )
    subject_id: str = Field(description="Customer ID for churn/QoE or transaction ID for fraud.")
    class_id: int | None = Field(
        description="Raw integer class returned by the artifact; null for QoE scores."
    )
    is_fraud: bool | None = Field(
        description="Fraud interpretation from the verified fraud class map; null for other models."
    )
    score: float | None = Field(
        ge=0,
        le=100,
        description="QoE score on the model's 0–100 scale; null for classifiers.",  # noqa: RUF001
    )
    model_version: str = Field(
        description="Deployment artifact version recorded at inference time."
    )
    created_at: datetime = Field(description="UTC persistence time.")


class PredictionResultPage(CommonModel):
    items: list[PredictionResult]
    next_cursor: str | None


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "The request failed validation."},
    401: {"model": ErrorEnvelope, "description": "Authentication is required."},
    403: {"model": ErrorEnvelope, "description": "The caller is not authorized."},
    404: {"model": ErrorEnvelope, "description": "The scoring run was not found."},
    409: {
        "model": ErrorEnvelope,
        "description": "The requested operation conflicts with run state.",
    },
    500: {"model": ErrorEnvelope, "description": "The request could not be completed."},
    503: {"model": ErrorEnvelope, "description": "A required service is temporarily unavailable."},
}


def _service(request: Request) -> ScoringService:
    return cast(ScoringService, request.app.state.scoring_service)


def _progress(run: ScoringRun, model: ModelFamily) -> ModelProgress:
    processed = int(getattr(run, f"{model.value}_processed_rows"))
    total = int(getattr(run, f"{model.value}_total_rows"))
    if run.status == ScoringRunStatus.QUEUED:
        status = ScoringRunStatus.QUEUED
    elif run.status == ScoringRunStatus.FAILED and processed < total:
        status = ScoringRunStatus.FAILED
    elif processed >= total:
        status = ScoringRunStatus.COMPLETED
    else:
        status = ScoringRunStatus.RUNNING
    return ModelProgress(status=status.value, processed_rows=processed, total_rows=total)


def _run_response(run: ScoringRun) -> ScoringRunResponse:
    return ScoringRunResponse(
        run_id=run.run_id,
        status=run.status.value,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        progress=ScoringRunProgress(
            churn=_progress(run, ModelFamily.CHURN),
            fraud=_progress(run, ModelFamily.FRAUD),
            qoe=_progress(run, ModelFamily.QOE),
        ),
        error_code=run.error_code,
    )


@router.get(
    "/scoring-runs",
    operation_id="listScoringRuns",
    summary="List scoring runs",
    description="Returns recent explicitly requested runs; it never starts a run.",
    response_model=ScoringRunPage,
    responses={k: v for k, v in _ERROR_RESPONSES.items() if k not in {404, 409}},
    openapi_extra={
        "x-side-effects": "None.",
        "x-error-codes": [
            "VALIDATION_ERROR",
            "AUTHENTICATION_OR_AUTHORIZATION",
            "UPSTREAM_UNAVAILABLE",
            "INTERNAL_ERROR",
        ],
        "x-timeouts-and-retries": (
            "10 second deadline; retry 503 at most twice with exponential backoff from 1 to 8 "
            "seconds."
        ),
        "x-ordering": (
            "created_at descending, then run_id ascending; cursor pagination is stable for "
            "immutable sort keys."
        ),
        "x-versioning": "/api/v1; breaking response changes require /api/v2.",
    },
)
async def list_scoring_runs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
) -> ScoringRunPage:
    try:
        async with asyncio.timeout(API_READ_DEADLINE_SECONDS):
            rows, next_cursor = await _service(request).list_runs(limit, cursor)
    except TimeoutError as exc:
        raise _deadline_error("scoring-run list") from exc
    return ScoringRunPage(items=[_run_response(row) for row in rows], next_cursor=next_cursor)


@router.post(
    "/scoring-runs",
    operation_id="createScoringRun",
    summary="Queue a scoring run",
    description=(
        "Queues churn, fraud, and QoE scoring over the configured source snapshot. "
        "The request has no body in v1. Repeating the same Idempotency-Key returns "
        "the original run; a distinct key while another run is active returns 409."
    ),
    status_code=202,
    response_model=ScoringRunAccepted,
    responses={
        202: {
            "model": ScoringRunAccepted,
            "description": "The run was durably queued.",
            "headers": {
                "Location": {
                    "description": "URL of the created run.",
                    "schema": {"type": "string", "format": "uri-reference"},
                }
            },
        },
        **{key: value for key, value in _ERROR_RESPONSES.items() if key != 404},
    },
    openapi_extra={
        "x-side-effects": (
            "Creates one durable scoring_runs row. A repeated Idempotency-Key returns that "
            "same row. A different key is rejected while a run is queued or running."
        ),
        "x-error-codes": [
            "VALIDATION_ERROR",
            "AUTHENTICATION_OR_AUTHORIZATION",
            "RUN_ALREADY_ACTIVE",
            "UPSTREAM_UNAVAILABLE",
            "INTERNAL_ERROR",
        ],
        "x-timeouts-and-retries": (
            "Return 202 within 5 seconds after durable acceptance. Retry 503 at most twice with "
            "exponential backoff from 1 to 8 seconds, honoring Retry-After; a retry must reuse "
            "the same Idempotency-Key."
        ),
        "x-ordering": "Runs are independent records; only one run may be active at a time.",
        "x-versioning": "/api/v1; breaking request or response changes require /api/v2.",
    },
)
async def create_scoring_run(
    request: Request,
    background_tasks: BackgroundTasks,
    response: Response,
    idempotency_key: UUID = Header(  # noqa: B008
        ..., alias="Idempotency-Key", description="UUID retained by the client for all retries."
    ),
) -> ScoringRunAccepted:
    service = _service(request)
    try:
        async with asyncio.timeout(SCORING_CREATE_DEADLINE_SECONDS):
            run, created = await service.create_run(idempotency_key)
    except TimeoutError as exc:
        raise _deadline_error("scoring-run acceptance") from exc
    if created:
        background_tasks.add_task(service.process_run, run.run_id)
    response.headers["Location"] = f"/api/v1/scoring-runs/{run.run_id}"
    return ScoringRunAccepted(run_id=run.run_id, status="queued", created_at=run.created_at)


@router.get(
    "/scoring-runs/{run_id}",
    operation_id="getScoringRun",
    summary="Get scoring-run status and progress",
    response_model=ScoringRunResponse,
    responses={key: value for key, value in _ERROR_RESPONSES.items() if key != 409},
    openapi_extra={
        "x-side-effects": "None.",
        "x-error-codes": [
            "VALIDATION_ERROR",
            "AUTHENTICATION_OR_AUTHORIZATION",
            "NOT_FOUND",
            "UPSTREAM_UNAVAILABLE",
            "INTERNAL_ERROR",
        ],
        "x-timeouts-and-retries": (
            "10 second deadline; retry 503 at most twice with exponential backoff from 1 to 8 "
            "seconds."
        ),
        "x-ordering": (
            "Progress is monotonic within a run; each response is a point-in-time snapshot."
        ),
        "x-versioning": "/api/v1; breaking response changes require /api/v2.",
    },
)
async def get_scoring_run(run_id: UUID, request: Request) -> ScoringRunResponse:
    try:
        async with asyncio.timeout(API_READ_DEADLINE_SECONDS):
            run = await _service(request).get_run(run_id)
    except TimeoutError as exc:
        raise _deadline_error("scoring-run status read") from exc
    if run is None:
        raise ServiceError(ErrorCode.NOT_FOUND, "Scoring run was not found.")
    return _run_response(run)


@router.get(
    "/scoring-runs/{run_id}/results",
    operation_id="listScoringRunResults",
    summary="List persisted prediction results",
    description="Results become pageable after the run is completed.",
    response_model=PredictionResultPage,
    responses=_ERROR_RESPONSES,
    openapi_extra={
        "x-side-effects": "None.",
        "x-error-codes": [
            "VALIDATION_ERROR",
            "AUTHENTICATION_OR_AUTHORIZATION",
            "NOT_FOUND",
            "RUN_NOT_COMPLETE",
            "UPSTREAM_UNAVAILABLE",
            "INTERNAL_ERROR",
        ],
        "x-timeouts-and-retries": (
            "10 second deadline; retry 503 at most twice with exponential backoff from 1 to 8 "
            "seconds."
        ),
        "x-ordering": (
            "model_type ascending, then subject_id ascending; cursor is opaque and stable for "
            "a completed run."
        ),
        "x-versioning": "/api/v1; breaking response changes require /api/v2.",
    },
)
async def list_scoring_run_results(
    run_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
) -> PredictionResultPage:
    try:
        async with asyncio.timeout(API_READ_DEADLINE_SECONDS):
            rows, next_cursor = await _service(request).list_results(run_id, limit, cursor)
    except TimeoutError as exc:
        raise _deadline_error("scoring-result read") from exc
    items = [
        PredictionResult(
            prediction_id=row.prediction_id,
            run_id=row.scoring_run_id,
            model_type=row.model_type.value,
            subject_id=row.customer_id or row.transaction_id or "",
            class_id=row.class_id,
            is_fraud=row.is_fraud,
            score=float(row.score) if row.score is not None else None,
            model_version=row.model_version,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return PredictionResultPage(items=items, next_cursor=next_cursor)
