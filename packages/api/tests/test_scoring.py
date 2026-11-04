from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from api.db.models import ModelFamily, ScoringRun, ScoringRunStatus
from api.routes.scoring import create_scoring_run
from api.services.mcp_clients import McpClients
from api.services.scoring import ScoringService, _validate_predictions
from data_fetch_tool import FetchCustomerDataInput, FetchCustomerDataOutput
from fastapi import BackgroundTasks, FastAPI, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable

from common.errors import ErrorCode, UpstreamError


class FakeScoringService:
    def __init__(self) -> None:
        self.runs: dict[UUID, ScoringRun] = {}
        self.created_keys: list[UUID] = []
        self.processed_ids: list[UUID] = []

    async def create_run(self, idempotency_key: UUID) -> tuple[ScoringRun, bool]:
        self.created_keys.append(idempotency_key)
        run = ScoringRun(
            run_id=uuid4(),
            idempotency_key=str(idempotency_key),
            request_fingerprint="a" * 64,
            status=ScoringRunStatus.QUEUED,
            churn_processed_rows=0,
            churn_total_rows=0,
            fraud_processed_rows=0,
            fraud_total_rows=0,
            qoe_processed_rows=0,
            qoe_total_rows=0,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            updated_at=datetime.now(UTC),
        )
        self.runs[run.run_id] = run
        return run, True

    async def process_run(self, run_id: UUID) -> None:
        self.processed_ids.append(run_id)


class FakeScalars:
    def __init__(self, values: Sequence[UUID]) -> None:
        self._values = values

    def all(self) -> Sequence[UUID]:
        return self._values


class FakeResumeSession:
    def __init__(self, accepted_run_ids: Sequence[UUID]) -> None:
        self.accepted_run_ids = accepted_run_ids
        self.statement: Executable | None = None

    async def __aenter__(self) -> FakeResumeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def scalars(self, statement: Executable) -> FakeScalars:
        self.statement = statement
        return FakeScalars(self.accepted_run_ids)


class FakeResumeSessionFactory:
    def __init__(self, session: FakeResumeSession) -> None:
        self.session = session

    def __call__(self) -> FakeResumeSession:
        return self.session


class FakeMcp:
    def __init__(self, outputs: Sequence[FetchCustomerDataOutput]) -> None:
        self.outputs = list(outputs)
        self.fetch_requests: list[FetchCustomerDataInput] = []
        self.prediction_calls = 0

    async def fetch_customer_data(
        self,
        request: FetchCustomerDataInput,
    ) -> FetchCustomerDataOutput:
        self.fetch_requests.append(request)
        return self.outputs.pop(0)

    async def predict_churn(self, customer_ids: list[str]) -> dict[str, object]:
        del customer_ids
        self.prediction_calls += 1
        return {"predictions": []}

    async def predict_fraud(self, transaction_ids: list[str]) -> dict[str, object]:
        del transaction_ids
        self.prediction_calls += 1
        return {"predictions": []}

    async def predict_qoe(self, customer_ids: list[str]) -> dict[str, object]:
        del customer_ids
        self.prediction_calls += 1
        return {"predictions": []}


def _scoring_service(
    *,
    session_factory: object | None = None,
    mcp: FakeMcp | None = None,
    batch_size: int = 10,
) -> ScoringService:
    return ScoringService(
        cast(async_sessionmaker[AsyncSession], session_factory or object()),
        cast(AsyncEngine, object()),
        cast(McpClients, mcp or FakeMcp([])),
        batch_size,
    )


@pytest.mark.asyncio
async def test_scoring_request_persists_queued_run_and_schedules_processing() -> None:
    app = FastAPI()
    fake_service = FakeScoringService()
    app.state.scoring_service = fake_service
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/scoring-runs",
            "raw_path": b"/api/v1/scoring-runs",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1234),
            "app": app,
        }
    )
    background_tasks = BackgroundTasks()
    response = Response()
    idempotency_key = uuid4()

    accepted = await create_scoring_run(
        request,
        background_tasks,
        response,
        idempotency_key=idempotency_key,
    )

    assert accepted.status == "queued"
    assert accepted.run_id in fake_service.runs
    assert fake_service.runs[accepted.run_id].status == ScoringRunStatus.QUEUED
    assert fake_service.created_keys == [idempotency_key]
    assert response.headers["Location"] == f"/api/v1/scoring-runs/{accepted.run_id}"
    assert len(background_tasks.tasks) == 1
    assert fake_service.processed_ids == []


@pytest.mark.asyncio
async def test_scoring_create_deadline_returns_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowScoringService(FakeScoringService):
        async def create_run(self, idempotency_key: UUID) -> tuple[ScoringRun, bool]:
            await asyncio.sleep(0.05)
            return await super().create_run(idempotency_key)

    monkeypatch.setattr("api.routes.scoring.SCORING_CREATE_DEADLINE_SECONDS", 0.001)
    app = FastAPI()
    app.state.scoring_service = SlowScoringService()
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/scoring-runs",
            "raw_path": b"/api/v1/scoring-runs",
            "query_string": b"",
            "headers": [],
            "server": ("test", 80),
            "client": ("test", 1234),
            "app": app,
        }
    )

    with pytest.raises(UpstreamError) as raised:
        await create_scoring_run(
            request,
            BackgroundTasks(),
            Response(),
            idempotency_key=uuid4(),
        )

    assert raised.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert raised.value.retryable


@pytest.mark.asyncio
async def test_resume_processes_only_persisted_accepted_run_ids() -> None:
    accepted_run_id = uuid4()
    session = FakeResumeSession([accepted_run_id])
    service = _scoring_service(session_factory=FakeResumeSessionFactory(session))
    processed: list[UUID] = []

    async def record_processing(run_id: UUID) -> None:
        processed.append(run_id)

    service.process_run = record_processing  # type: ignore[method-assign]
    await service.resume_unfinished()

    assert processed == [accepted_run_id]
    assert "app.scoring_runs.status IN" in str(session.statement)


@pytest.mark.asyncio
async def test_empty_startup_resume_does_not_call_prediction_tools() -> None:
    session = FakeResumeSession([])
    mcp = FakeMcp([])
    service = _scoring_service(session_factory=FakeResumeSessionFactory(session), mcp=mcp)

    await service.resume_unfinished()

    assert mcp.prediction_calls == 0
    assert mcp.fetch_requests == []


@pytest.mark.asyncio
async def test_data_fetch_page_requests_use_the_configured_batch_bound() -> None:
    mcp = FakeMcp(
        [
            FetchCustomerDataOutput(records=[], next_cursor="page-2"),
            FetchCustomerDataOutput(records=[], next_cursor=None),
        ]
    )
    service = _scoring_service(mcp=mcp, batch_size=7)

    pages = [
        page
        async for page in service._iter_source_records(
            "customers",
            "customer_id",
            "churn_features",
            [{"field": "dollar_weight", "operator": "gte", "value": 0}],
        )
    ]

    assert pages == [[], []]
    assert [request.limit for request in mcp.fetch_requests] == [7, 7]
    assert mcp.fetch_requests[1].cursor == "page-2"


@pytest.mark.asyncio
async def test_repeated_cursor_cycle_is_rejected() -> None:
    mcp = FakeMcp(
        [
            FetchCustomerDataOutput(records=[], next_cursor="cursor-a"),
            FetchCustomerDataOutput(records=[], next_cursor="cursor-b"),
            FetchCustomerDataOutput(records=[], next_cursor="cursor-a"),
        ]
    )
    service = _scoring_service(mcp=mcp, batch_size=7)

    with pytest.raises(UpstreamError) as caught:
        [
            page
            async for page in service._iter_source_records(
                "customers",
                "customer_id",
                "churn_features",
                [{"field": "dollar_weight", "operator": "gte", "value": 0}],
            )
        ]

    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert caught.value.retryable is False
    assert [request.cursor for request in mcp.fetch_requests] == [None, "cursor-a", "cursor-b"]


def test_batch_size_above_frozen_data_fetch_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        _scoring_service(batch_size=101)


def test_prediction_validation_rejects_missing_or_unknown_subjects() -> None:
    with pytest.raises(UpstreamError) as missing:
        _validate_predictions(ModelFamily.CHURN, ["customer-1"], {"predictions": []})
    with pytest.raises(UpstreamError) as unknown:
        _validate_predictions(
            ModelFamily.CHURN,
            ["customer-1"],
            {"predictions": [{"customer_id": "other", "class_id": 1, "model_version": "v1"}]},
        )

    assert missing.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert unknown.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
