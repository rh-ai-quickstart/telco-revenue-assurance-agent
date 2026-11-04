"""Read-only aggregate metrics for a selected persisted scoring run."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from common.errors import ErrorCode, ErrorEnvelope, ServiceError
from common.models import CommonModel

from ..db.models import Customer, ModelFamily, Prediction, ScoringRun

router = APIRouter(tags=["Metrics"])
API_READ_DEADLINE_SECONDS = 10.0


class ClassAggregate(CommonModel):
    class_id: int = Field(description="Raw class ID emitted by the model artifact.")
    prediction_count: int = Field(ge=0, description="Persisted result count for this class.")
    dollar_weight_sum: float | None = Field(
        ge=0,
        description="Sum of customer dollar_weight for churn; null for fraud.",
    )


class ChurnMetrics(CommonModel):
    customer_count: int = Field(ge=0, description="Number of persisted churn results.")
    classes: list[ClassAggregate] = Field(
        description="Count and customer dollar weight grouped by raw artifact class ID."
    )


class FraudMetrics(CommonModel):
    transaction_count: int = Field(ge=0, description="Number of persisted fraud results.")
    fraud_count: int = Field(
        ge=0,
        description="Count of class 1, mapped to Fraud by the artifact class order [False, True].",
    )
    classes: list[ClassAggregate] = Field(description="Count grouped by fraud artifact class ID.")


class QoeMetrics(CommonModel):
    customer_count: int = Field(ge=0, description="Number of persisted QoE scores.")
    mean_score: float | None = Field(
        ge=0,
        le=100,
        description="Arithmetic mean over persisted scores, or null when none exist.",
    )
    minimum_score: float | None = Field(
        ge=0,
        le=100,
        description="Minimum persisted QoE score, or null when none exist.",
    )
    maximum_score: float | None = Field(
        ge=0,
        le=100,
        description="Maximum persisted QoE score, or null when none exist.",
    )


class RunMetrics(CommonModel):
    run_id: UUID = Field(description="Scoring run summarized by this response.")
    status: Literal["queued", "running", "completed", "failed"] = Field(
        description="Run state when the aggregation was read."
    )
    as_of: datetime = Field(description="UTC time at which the persisted results were aggregated.")
    churn: ChurnMetrics
    fraud: FraudMetrics
    qoe: QoeMetrics


@router.get(
    "/metrics",
    operation_id="getRunMetrics",
    summary="Get metrics for a scoring run",
    description=(
        "Aggregates the committed predictions for the selected run. Active runs return "
        "a partial snapshot; class IDs are shown without semantic churn labels until "
        "the deployed artifact class map is verified."
    ),
    response_model=RunMetrics,
    responses={
        400: {"model": ErrorEnvelope, "description": "The request failed validation."},
        401: {"model": ErrorEnvelope, "description": "Authentication is required."},
        403: {"model": ErrorEnvelope, "description": "The caller is not authorized."},
        404: {"model": ErrorEnvelope, "description": "The scoring run was not found."},
        500: {"model": ErrorEnvelope, "description": "The request could not be completed."},
        503: {
            "model": ErrorEnvelope,
            "description": "A required service is temporarily unavailable.",
        },
    },
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
        "x-ordering": "No ordering guarantee applies to aggregate fields.",
        "x-versioning": "/api/v1; breaking response changes require /api/v2.",
    },
)
async def get_run_metrics(
    request: Request,
    run_id: UUID = Query(  # noqa: B008
        ..., description="Scoring run whose persisted predictions are aggregated."
    ),
) -> RunMetrics:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    try:
        async with asyncio.timeout(API_READ_DEADLINE_SECONDS):
            async with session_factory() as session:
                run = await session.get(ScoringRun, run_id)
                if run is None:
                    raise ServiceError(ErrorCode.NOT_FOUND, "Scoring run was not found.")
                churn = await _churn_metrics(session, run_id)
                fraud = await _fraud_metrics(session, run_id)
                qoe = await _qoe_metrics(session, run_id)
    except TimeoutError as exc:
        raise ServiceError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The metrics read exceeded its request deadline.",
            retryable=True,
        ) from exc
    return RunMetrics(
        run_id=run.run_id,
        status=run.status.value,
        as_of=datetime.now(UTC),
        churn=churn,
        fraud=fraud,
        qoe=qoe,
    )


async def _churn_metrics(session: AsyncSession, run_id: UUID) -> ChurnMetrics:
    statement = (
        select(
            Prediction.class_id,
            func.count(Prediction.prediction_id),
            func.sum(Customer.dollar_weight),
        )
        .join(Customer, Prediction.customer_id == Customer.customer_id)
        .where(Prediction.scoring_run_id == run_id, Prediction.model_type == ModelFamily.CHURN)
        .group_by(Prediction.class_id)
        .order_by(Prediction.class_id)
    )
    rows = (await session.execute(statement)).all()
    classes = [
        ClassAggregate(
            class_id=cast(int, class_id),
            prediction_count=count,
            dollar_weight_sum=float(amount) if amount is not None else None,
        )
        for class_id, count, amount in rows
    ]
    return ChurnMetrics(
        customer_count=sum(item.prediction_count for item in classes), classes=classes
    )


async def _fraud_metrics(session: AsyncSession, run_id: UUID) -> FraudMetrics:
    statement = (
        select(Prediction.class_id, func.count(Prediction.prediction_id))
        .where(Prediction.scoring_run_id == run_id, Prediction.model_type == ModelFamily.FRAUD)
        .group_by(Prediction.class_id)
        .order_by(Prediction.class_id)
    )
    rows = (await session.execute(statement)).all()
    classes = [
        ClassAggregate(
            class_id=cast(int, class_id),
            prediction_count=int(count),
            dollar_weight_sum=None,
        )
        for class_id, count in rows
    ]
    return FraudMetrics(
        transaction_count=sum(item.prediction_count for item in classes),
        fraud_count=sum(item.prediction_count for item in classes if item.class_id == 1),
        classes=classes,
    )


async def _qoe_metrics(session: AsyncSession, run_id: UUID) -> QoeMetrics:
    statement = select(
        func.count(Prediction.prediction_id),
        func.avg(Prediction.score),
        func.min(Prediction.score),
        func.max(Prediction.score),
    ).where(Prediction.scoring_run_id == run_id, Prediction.model_type == ModelFamily.QOE)
    count, mean_score, minimum_score, maximum_score = (await session.execute(statement)).one()
    return QoeMetrics(
        customer_count=cast(int, count),
        mean_score=float(cast(Decimal, mean_score)) if mean_score is not None else None,
        minimum_score=float(cast(Decimal, minimum_score)) if minimum_score is not None else None,
        maximum_score=float(cast(Decimal, maximum_score)) if maximum_score is not None else None,
    )
