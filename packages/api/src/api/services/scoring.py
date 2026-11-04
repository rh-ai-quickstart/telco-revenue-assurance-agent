"""Durable scoring-run admission, bounded processing, and result pagination."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from data_fetch_tool import FetchCustomerDataInput, FetchCustomerDataOutput
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from common.errors import ErrorCode, ErrorDetail, ServiceError, UpstreamError

from ..db.models import ModelFamily, Prediction, ScoringRun, ScoringRunStatus
from .mcp_clients import McpClients

logger = logging.getLogger(__name__)

_SCORING_RUN_LOCK_ID = 731_590_204_117
_RUN_FINGERPRINT = hashlib.sha256(b"score-all-models-v1").hexdigest()


class ScoringService:
    """Accept analyst-triggered work and process it with durable checkpoints."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        mcp_clients: McpClients,
        batch_size: int,
    ) -> None:
        if not 1 <= batch_size <= 100:
            raise ValueError("Scoring batch size must be between 1 and 100.")
        self._session_factory = session_factory
        self._engine = engine
        self._mcp = mcp_clients
        self._batch_size = batch_size

    async def create_run(self, idempotency_key: UUID) -> tuple[ScoringRun, bool]:
        """Persist one requested run; return its row and whether this call created it."""

        key = str(idempotency_key)
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(select(func.pg_advisory_xact_lock(_SCORING_RUN_LOCK_ID)))
                existing = await session.scalar(
                    select(ScoringRun).where(ScoringRun.idempotency_key == key)
                )
                if existing is not None:
                    return existing, False
                active = await session.scalar(
                    select(ScoringRun)
                    .where(
                        ScoringRun.status.in_([ScoringRunStatus.QUEUED, ScoringRunStatus.RUNNING])
                    )
                    .order_by(ScoringRun.created_at, ScoringRun.run_id)
                    .limit(1)
                    .with_for_update()
                )
                if active is not None:
                    raise ServiceError(
                        ErrorCode.RUN_ALREADY_ACTIVE,
                        "A scoring run is already active.",
                        details={"current_run_id": str(active.run_id)},
                        rest_details=[
                            ErrorDetail(
                                field=None,
                                reason=(
                                    "Wait for the active run to finish before creating another run."
                                ),
                                limit=None,
                                current_run_id=active.run_id,
                            )
                        ],
                    )
                run = ScoringRun(
                    idempotency_key=key,
                    request_fingerprint=_RUN_FINGERPRINT,
                    status=ScoringRunStatus.QUEUED,
                    churn_processed_rows=0,
                    churn_total_rows=0,
                    fraud_processed_rows=0,
                    fraud_total_rows=0,
                    qoe_processed_rows=0,
                    qoe_total_rows=0,
                )
                session.add(run)
                await session.flush()
            return run, True

    async def get_run(self, run_id: UUID) -> ScoringRun | None:
        async with self._session_factory() as session:
            return await session.get(ScoringRun, run_id)

    async def list_runs(
        self, limit: int, cursor: str | None
    ) -> tuple[list[ScoringRun], str | None]:
        position = _decode_run_cursor(cursor) if cursor is not None else None
        statement = select(ScoringRun)
        if position is not None:
            created_at, last_run_id = position
            statement = statement.where(
                or_(
                    ScoringRun.created_at < created_at,
                    and_(ScoringRun.created_at == created_at, ScoringRun.run_id > last_run_id),
                )
            )
        statement = statement.order_by(ScoringRun.created_at.desc(), ScoringRun.run_id.asc()).limit(
            limit + 1
        )
        async with self._session_factory() as session:
            rows = list((await session.scalars(statement)).all())
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = _encode_run_cursor(page[-1]) if has_more and page else None
        return page, next_cursor

    async def list_results(
        self, run_id: UUID, limit: int, cursor: str | None
    ) -> tuple[list[Prediction], str | None]:
        position = _decode_result_cursor(cursor) if cursor is not None else None
        async with self._session_factory() as session:
            run = await session.get(ScoringRun, run_id)
            if run is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "Scoring run was not found.")
            if run.status != ScoringRunStatus.COMPLETED:
                raise ServiceError(
                    ErrorCode.RUN_NOT_COMPLETE,
                    "Results are pageable after the run completes.",
                    retryable=True,
                    details={"current_run_id": str(run_id)},
                )
            subject_id = func.coalesce(Prediction.customer_id, Prediction.transaction_id)
            statement = select(Prediction).where(Prediction.scoring_run_id == run_id)
            if position is not None:
                last_model, last_subject = position
                statement = statement.where(
                    or_(
                        Prediction.model_type > last_model,
                        and_(Prediction.model_type == last_model, subject_id > last_subject),
                    )
                )
            statement = statement.order_by(Prediction.model_type, subject_id).limit(limit + 1)
            rows = list((await session.scalars(statement)).all())
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = _encode_result_cursor(page[-1]) if has_more and page else None
        return page, next_cursor

    async def resume_unfinished(self) -> None:
        """Resume only durable runs that were explicitly accepted before a restart."""

        async with self._session_factory() as session:
            run_ids = list(
                (
                    await session.scalars(
                        select(ScoringRun.run_id)
                        .where(
                            ScoringRun.status.in_(
                                [ScoringRunStatus.QUEUED, ScoringRunStatus.RUNNING]
                            )
                        )
                        .order_by(ScoringRun.created_at, ScoringRun.run_id)
                    )
                ).all()
            )
        for run_id in run_ids:
            await self.process_run(run_id)

    async def process_run(self, run_id: UUID) -> None:
        """Run one accepted job, protected by a PostgreSQL session advisory lock."""

        first_lock = (run_id.int >> 96) & 0x7FFFFFFF
        second_lock = run_id.int & 0x7FFFFFFF
        lock_statement = select(func.pg_try_advisory_lock(first_lock, second_lock))
        unlock_statement = select(func.pg_advisory_unlock(first_lock, second_lock))
        async with self._engine.connect() as lock_connection:
            acquired = await lock_connection.scalar(lock_statement)
            if not acquired:
                return
            try:
                await self._process_run_locked(run_id)
            except UpstreamError as exc:
                logger.warning(
                    "scoring run failed at an upstream boundary",
                    extra={"component": "api", "run_id": str(run_id), "error_code": exc.code.value},
                )
                await self._finish_run(run_id, ScoringRunStatus.FAILED, exc.code.value)
            except Exception:
                logger.exception(
                    "scoring run failed unexpectedly",
                    extra={"component": "api", "run_id": str(run_id)},
                )
                await self._finish_run(
                    run_id, ScoringRunStatus.FAILED, ErrorCode.INTERNAL_ERROR.value
                )
            finally:
                await lock_connection.scalar(unlock_statement)

    async def _process_run_locked(self, run_id: UUID) -> None:
        async with self._session_factory() as session:
            run = await session.get(ScoringRun, run_id, with_for_update=True)
            if run is None or run.status in {
                ScoringRunStatus.COMPLETED,
                ScoringRunStatus.FAILED,
            }:
                return
            run.status = ScoringRunStatus.RUNNING
            run.started_at = run.started_at or datetime.now(UTC)
            run.updated_at = datetime.now(UTC)
            await session.commit()

        await self._process_model(run_id, ModelFamily.CHURN)
        await self._process_model(run_id, ModelFamily.FRAUD)
        await self._process_model(run_id, ModelFamily.QOE)
        await self._finish_run(run_id, ScoringRunStatus.COMPLETED, None)

    async def _process_model(self, run_id: UUID, model: ModelFamily) -> None:
        entity, id_alias, feature_alias, filters = _model_source(model)
        total = await self._count_source_subjects(
            entity,
            id_alias,
            feature_alias if model == ModelFamily.QOE else None,
            filters,
        )
        await self._set_total(run_id, model, total)

        async for records in self._iter_source_records(entity, id_alias, feature_alias, filters):
            snapshots: dict[str, dict[str, object]] = {}
            for record in records:
                subject_id = record.get("record_id")
                attributes = record.get("attributes")
                if not isinstance(subject_id, str) or not isinstance(attributes, Mapping):
                    raise UpstreamError(
                        ErrorCode.UPSTREAM_UNAVAILABLE,
                        "The data-fetch service returned an invalid source record.",
                    )
                features = _feature_map(attributes.get(feature_alias))
                if model == ModelFamily.QOE and not features:
                    continue
                if not features:
                    raise UpstreamError(
                        ErrorCode.FEATURE_SCHEMA_MISMATCH,
                        "A source row is missing the configured model feature snapshot.",
                    )
                snapshots[subject_id] = features
            if not snapshots:
                continue

            existing = await self._existing_subject_ids(run_id, model, list(snapshots))
            pending = [subject_id for subject_id in snapshots if subject_id not in existing]
            if pending:
                response = await self._predict(model, pending)
                predictions = _validate_predictions(model, pending, response)
                rows = [
                    _prediction_row(run_id, model, prediction, snapshots)
                    for prediction in predictions
                ]
                await self._persist_predictions(run_id, model, rows)
            await self._refresh_processed(run_id, model)

        processed, persisted_total = await self._progress_counts(run_id, model)
        if processed != persisted_total or persisted_total != total:
            raise UpstreamError(
                ErrorCode.FEATURE_SCHEMA_MISMATCH,
                "The source subject count changed while the scoring run was processing.",
            )

    async def _count_source_subjects(
        self,
        entity: str,
        id_alias: str,
        feature_alias: str | None,
        filters: list[dict[str, object]],
    ) -> int:
        count = 0
        async for records in self._iter_source_records(entity, id_alias, feature_alias, filters):
            if feature_alias is None:
                count += len(records)
            else:
                count += sum(
                    bool(
                        _feature_map(
                            cast(Mapping[str, object], record.get("attributes")).get(feature_alias)
                        )
                    )
                    for record in records
                    if isinstance(record.get("attributes"), Mapping)
                )
        return count

    async def _iter_source_records(
        self,
        entity: str,
        id_alias: str,
        feature_alias: str | None,
        filters: list[dict[str, object]],
    ) -> AsyncIterator[list[dict[str, object]]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            aliases = [id_alias]
            if feature_alias is not None:
                aliases.append(feature_alias)
            request_payload: dict[str, object] = {
                "entity": entity,
                "field_aliases": aliases,
                "filters": filters,
                "limit": self._batch_size,
            }
            if cursor is not None:
                request_payload["cursor"] = cursor
            request = FetchCustomerDataInput.model_validate(request_payload)
            response: FetchCustomerDataOutput = await self._mcp.fetch_customer_data(request)
            records = [
                cast(dict[str, object], record.model_dump(mode="python"))
                for record in response.records
            ]
            yield records
            next_cursor = response.next_cursor
            if next_cursor is None:
                return
            if next_cursor in seen_cursors:
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    "The data-fetch service returned a repeated page cursor.",
                    retryable=False,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    async def _predict(self, model: ModelFamily, subject_ids: list[str]) -> dict[str, object]:
        if model == ModelFamily.CHURN:
            return await self._mcp.predict_churn(subject_ids)
        if model == ModelFamily.FRAUD:
            return await self._mcp.predict_fraud(subject_ids)
        return await self._mcp.predict_qoe(subject_ids)

    async def _existing_subject_ids(
        self, run_id: UUID, model: ModelFamily, subject_ids: Sequence[str]
    ) -> set[str]:
        if not subject_ids:
            return set()
        subject_column = (
            Prediction.transaction_id if model == ModelFamily.FRAUD else Prediction.customer_id
        )
        async with self._session_factory() as session:
            found = await session.scalars(
                select(subject_column).where(
                    Prediction.scoring_run_id == run_id,
                    Prediction.model_type == model,
                    subject_column.in_(subject_ids),
                )
            )
            return {value for value in found.all() if value is not None}

    async def _persist_predictions(
        self, run_id: UUID, model: ModelFamily, rows: list[dict[str, object]]
    ) -> None:
        if not rows:
            return
        subject_column = (
            Prediction.transaction_id if model == ModelFamily.FRAUD else Prediction.customer_id
        )
        statement = (
            pg_insert(Prediction)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=[Prediction.scoring_run_id, Prediction.model_type, subject_column],
                index_where=subject_column.is_not(None),
            )
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def _refresh_processed(self, run_id: UUID, model: ModelFamily) -> None:
        processed_column = getattr(ScoringRun, f"{model.value}_processed_rows")
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(Prediction.prediction_id)).where(
                    Prediction.scoring_run_id == run_id,
                    Prediction.model_type == model,
                )
            )
            run = await session.get(ScoringRun, run_id)
            if run is None:
                return
            setattr(run, processed_column.key, int(count or 0))
            run.updated_at = datetime.now(UTC)
            await session.commit()

    async def _set_total(self, run_id: UUID, model: ModelFamily, total: int) -> None:
        total_column = getattr(ScoringRun, f"{model.value}_total_rows")
        async with self._session_factory() as session:
            run = await session.get(ScoringRun, run_id)
            if run is None:
                return
            setattr(run, total_column.key, total)
            run.updated_at = datetime.now(UTC)
            await session.commit()

    async def _progress_counts(self, run_id: UUID, model: ModelFamily) -> tuple[int, int]:
        async with self._session_factory() as session:
            run = await session.get(ScoringRun, run_id)
            if run is None:
                return 0, 0
            return (
                int(getattr(run, f"{model.value}_processed_rows")),
                int(getattr(run, f"{model.value}_total_rows")),
            )

    async def _finish_run(
        self,
        run_id: UUID,
        status: ScoringRunStatus,
        error_code: str | None,
    ) -> None:
        async with self._session_factory() as session:
            run = await session.get(ScoringRun, run_id, with_for_update=True)
            if run is None or run.status in {
                ScoringRunStatus.COMPLETED,
                ScoringRunStatus.FAILED,
            }:
                return
            now = datetime.now(UTC)
            run.status = status
            run.error_code = error_code
            run.completed_at = now
            run.updated_at = now
            await session.commit()


def _model_source(
    model: ModelFamily,
) -> tuple[str, str, str, list[dict[str, object]]]:
    if model in {ModelFamily.CHURN, ModelFamily.QOE}:
        feature_alias = "churn_features" if model == ModelFamily.CHURN else "qoe_features"
        return (
            "customers",
            "customer_id",
            feature_alias,
            [{"field": "dollar_weight", "operator": "gte", "value": 0}],
        )
    return (
        "transactions",
        "transaction_id",
        "fraud_features",
        [{"field": "is_fraud", "operator": "in", "value": [True, False]}],
    )


def _feature_map(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _validate_predictions(
    model: ModelFamily,
    requested_ids: list[str],
    response: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_predictions = response.get("predictions")
    if not isinstance(raw_predictions, list):
        raise UpstreamError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "A prediction tool returned an invalid result.",
        )
    id_field = "transaction_id" if model == ModelFamily.FRAUD else "customer_id"
    validated: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_predictions:
        if not isinstance(raw, Mapping):
            raise UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE, "A prediction result was invalid.")
        subject_id = raw.get(id_field)
        version = raw.get("model_version")
        if not isinstance(subject_id, str) or subject_id not in requested_ids:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "A prediction result had an unknown subject."
            )
        if subject_id in seen:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "A prediction result duplicated a subject."
            )
        if not isinstance(version, str) or not version:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE, "A prediction result omitted its model version."
            )
        result = {str(key): value for key, value in raw.items() if isinstance(key, str)}
        if model == ModelFamily.CHURN:
            if not isinstance(result.get("class_id"), int) or isinstance(
                result.get("class_id"), bool
            ):
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE, "A churn result had an invalid class ID."
                )
        elif model == ModelFamily.FRAUD:
            class_id = result.get("class_id")
            is_fraud = result.get("is_fraud")
            if (
                class_id not in {0, 1}
                or not isinstance(is_fraud, bool)
                or is_fraud != (class_id == 1)
            ):
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE, "A fraud result violated its class mapping."
                )
        else:
            score = result.get("score")
            if (
                not isinstance(score, int | float)
                or isinstance(score, bool)
                or not 0 <= score <= 100
            ):
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE, "A QoE result had an invalid score."
                )
        seen.add(subject_id)
        validated.append(result)
    if seen != set(requested_ids):
        raise UpstreamError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "A prediction tool did not return every requested subject.",
        )
    return validated


def _prediction_row(
    run_id: UUID,
    model: ModelFamily,
    result: dict[str, object],
    snapshots: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    subject_field = "transaction_id" if model == ModelFamily.FRAUD else "customer_id"
    subject_id = cast(str, result[subject_field])
    snapshot = snapshots[subject_id]
    canonical_snapshot = json.dumps(
        _canonical_value(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    row: dict[str, object] = {
        "scoring_run_id": run_id,
        "model_type": model,
        "model_version": result["model_version"],
        "feature_snapshot_sha256": hashlib.sha256(canonical_snapshot.encode()).hexdigest(),
        "model_output": result,
    }
    if model == ModelFamily.FRAUD:
        row["transaction_id"] = subject_id
        row["class_id"] = result["class_id"]
        row["is_fraud"] = result["is_fraud"]
        row["score"] = None
        row["customer_id"] = None
    elif model == ModelFamily.CHURN:
        row["customer_id"] = subject_id
        row["class_id"] = result["class_id"]
        row["is_fraud"] = None
        row["score"] = None
        row["transaction_id"] = None
    else:
        row["customer_id"] = subject_id
        row["class_id"] = None
        row["is_fraud"] = None
        row["score"] = Decimal(str(result["score"]))
        row["transaction_id"] = None
    return row


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _encode_run_cursor(run: ScoringRun) -> str:
    return _encode_cursor(
        {"created_at": run.created_at.astimezone(UTC).isoformat(), "run_id": str(run.run_id)}
    )


def _decode_run_cursor(cursor: str) -> tuple[datetime, UUID]:
    payload = _decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(cast(str, payload["created_at"]))
        run_id = UUID(cast(str, payload["run_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.") from exc
    if created_at.tzinfo is None:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.")
    return created_at, run_id


def _encode_result_cursor(prediction: Prediction) -> str:
    subject_id = prediction.customer_id or prediction.transaction_id
    if subject_id is None:
        raise ServiceError(ErrorCode.INTERNAL_ERROR, "A persisted prediction had no subject ID.")
    return _encode_cursor({"model_type": prediction.model_type.value, "subject_id": subject_id})


def _decode_result_cursor(cursor: str) -> tuple[ModelFamily, str]:
    payload = _decode_cursor(cursor)
    try:
        model = ModelFamily(cast(str, payload["model_type"]))
        subject = cast(str, payload["subject_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.") from exc
    if not subject:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.")
    return model, subject


def _encode_cursor(payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, object]:
    if not 1 <= len(cursor) <= 1024:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ServiceError(ErrorCode.VALIDATION_ERROR, "cursor is invalid.")
    return cast(dict[str, object], value)
