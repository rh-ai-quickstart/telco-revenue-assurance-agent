"""Read-only async SQLAlchemy repository for the data-fetch MCP service."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from uuid import UUID

from pydantic import SecretStr, ValidationError
from sqlalchemy import and_, or_, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select

from common.errors import DatabaseError, ErrorCode, RequestError
from common.models import CommonModel
from data_fetch_tool.field_allowlist import (
    DEFAULT_FIELDS,
    ENTITY_ID_COLUMNS,
    ENTITY_TABLES,
    FIELD_ALLOWLIST,
    FILTER_ONLY_FIELDS,
    FieldSpec,
    feature_value_is_allowed,
)
from data_fetch_tool.schemas import (
    Entity,
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
    FetchRecord,
    FieldAlias,
    FilterInput,
    ScalarValue,
)

logger = logging.getLogger(__name__)

_READ_ONLY_TRANSACTION = text("SET TRANSACTION READ ONLY")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CONNECT_TIMEOUT_SECONDS = 5
_POOL_TIMEOUT_SECONDS = 5
_STATEMENT_TIMEOUT_MILLISECONDS = 5_000
_POOL_SIZE = 5


class _CursorState(CommonModel):
    version: Literal[1]
    entity: str
    fingerprint: str
    record_id: str
    sort_field: str | None
    direction: str | None
    sort_value: ScalarValue


@dataclass(frozen=True)
class PreparedFilter:
    alias: FieldAlias
    operator: str
    value: object
    spec: FieldSpec


@dataclass(frozen=True)
class PreparedRequest:
    entity: Entity
    output_aliases: tuple[FieldAlias, ...]
    filters: tuple[PreparedFilter, ...]
    sort_alias: FieldAlias | None
    sort_direction: Literal["asc", "desc"] | None
    sort_spec: FieldSpec | None
    fingerprint: str


def create_readonly_engine(database_readonly_url: SecretStr) -> AsyncEngine:
    """Create one bounded PostgreSQL async engine for the read-only role."""
    raw_url = database_readonly_url.get_secret_value()
    try:
        url: URL = make_url(raw_url)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise ValueError("DATABASE_READONLY_URL must be a valid async PostgreSQL DSN.") from exc
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_READONLY_URL must use the postgresql+asyncpg dialect.")

    return create_async_engine(
        url,
        connect_args={
            "timeout": _CONNECT_TIMEOUT_SECONDS,
            "command_timeout": _STATEMENT_TIMEOUT_MILLISECONDS / 1_000,
            "server_settings": {
                "statement_timeout": str(_STATEMENT_TIMEOUT_MILLISECONDS),
                "default_transaction_read_only": "on",
            },
        },
        pool_size=_POOL_SIZE,
        pool_timeout=_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
    )


def prepare_request(request: FetchCustomerDataInput, max_fetch_rows: int) -> PreparedRequest:
    """Validate aliases and request bounds before creating a database session."""
    if request.entity not in ENTITY_TABLES:
        raise RequestError(
            ErrorCode.INVALID_ENTITY,
            "Entity is not supported.",
            details={"entity": request.entity[:64]},
        )
    entity = cast(Entity, request.entity)
    if not 1 <= max_fetch_rows <= 100:
        raise ValueError("max_fetch_rows must be between 1 and 100.")
    if request.limit < 1 or request.limit > 100 or request.limit > max_fetch_rows:
        raise RequestError(
            ErrorCode.RESULT_LIMIT_EXCEEDED,
            "The requested row limit exceeds the configured bound.",
            details={"limit": request.limit, "maximum": max_fetch_rows},
        )
    if request.ids is not None:
        if not request.ids:
            raise RequestError(ErrorCode.INVALID_FILTER, "ids must not be empty.")
        if len(request.ids) > 100 or any(len(record_id) > 200 for record_id in request.ids):
            raise RequestError(
                ErrorCode.RESULT_LIMIT_EXCEEDED,
                "The identifier count or length exceeds the configured bound.",
                details={"maximum_count": 100, "maximum_length": 200},
            )
        if any(not record_id for record_id in request.ids):
            raise RequestError(ErrorCode.INVALID_FILTER, "Identifiers must not be empty.")
        if len(set(request.ids)) != len(request.ids):
            raise RequestError(ErrorCode.INVALID_FILTER, "ids must contain unique values.")
    if request.filters is not None and not 1 <= len(request.filters) <= 10:
        raise RequestError(
            ErrorCode.RESULT_LIMIT_EXCEEDED,
            "The filter count exceeds the configured bound.",
            details={"maximum": 10},
        )
    if request.ids is None and not request.filters:
        raise RequestError(
            ErrorCode.INVALID_FILTER,
            "Provide at least one of ids or filters.",
        )
    if request.cursor is not None and len(request.cursor) > 1024:
        raise RequestError(
            ErrorCode.RESULT_LIMIT_EXCEEDED,
            "The cursor exceeds the configured bound.",
            details={"maximum_length": 1024},
        )

    entity_fields = FIELD_ALLOWLIST[entity]
    filter_fields = {**entity_fields, **FILTER_ONLY_FIELDS.get(entity, {})}
    requested_aliases = request.field_aliases or list(DEFAULT_FIELDS[entity])
    if len(requested_aliases) > 19:
        raise RequestError(
            ErrorCode.RESULT_LIMIT_EXCEEDED,
            "The requested field count exceeds the configured bound.",
            details={"maximum": 19},
        )
    if len(requested_aliases) != len(set(requested_aliases)):
        raise RequestError(ErrorCode.INVALID_FIELD, "Field aliases must be unique.")

    output_aliases: list[FieldAlias] = []
    for requested_alias in requested_aliases:
        spec = entity_fields.get(cast(FieldAlias, requested_alias))
        if spec is None:
            raise RequestError(
                ErrorCode.INVALID_FIELD,
                "Field alias is not allowed for this entity.",
                details={"field": requested_alias[:64], "entity": entity},
            )
        output_aliases.append(cast(FieldAlias, requested_alias))

    prepared_filters: list[PreparedFilter] = []
    for request_filter in request.filters or []:
        prepared_filters.append(_prepare_filter(entity, filter_fields, request_filter))

    sort_alias: FieldAlias | None = None
    sort_direction: Literal["asc", "desc"] | None = None
    sort_spec: FieldSpec | None = None
    if request.sort is not None:
        raw_sort_alias = request.sort.field
        raw_direction = request.sort.direction
        sort_spec = entity_fields.get(cast(FieldAlias, raw_sort_alias))
        if sort_spec is None or sort_spec.kind == "feature_map":
            raise RequestError(
                ErrorCode.INVALID_FIELD,
                "Sort alias is not allowed for this entity.",
                details={"field": raw_sort_alias[:64], "entity": entity},
            )
        if raw_direction not in {"asc", "desc"}:
            raise RequestError(ErrorCode.INVALID_FILTER, "Sort direction must be asc or desc.")
        sort_alias = cast(FieldAlias, raw_sort_alias)
        sort_direction = cast(Literal["asc", "desc"], raw_direction)

    fingerprint = _request_fingerprint(request, entity, output_aliases)
    return PreparedRequest(
        entity=entity,
        output_aliases=tuple(output_aliases),
        filters=tuple(prepared_filters),
        sort_alias=sort_alias,
        sort_direction=sort_direction,
        sort_spec=sort_spec,
        fingerprint=fingerprint,
    )


def _prepare_filter(
    entity: Entity,
    entity_fields: Mapping[FieldAlias, FieldSpec],
    request_filter: FilterInput,
) -> PreparedFilter:
    raw_alias = request_filter.field
    spec = entity_fields.get(cast(FieldAlias, raw_alias))
    if spec is None or spec.kind == "feature_map":
        raise RequestError(
            ErrorCode.INVALID_FILTER,
            "Filter field is not allowed for this entity.",
            details={"field": raw_alias[:64], "entity": entity},
        )

    operator = request_filter.operator
    if operator not in {"eq", "in", "gte", "lte"}:
        raise RequestError(ErrorCode.INVALID_FILTER, "Filter operator is not supported.")
    if operator in {"gte", "lte"} and not spec.ordered:
        raise RequestError(
            ErrorCode.INVALID_FILTER,
            "Ordered comparisons require an ordered scalar field.",
            details={"field": raw_alias[:64]},
        )

    value = request_filter.value
    if operator == "in":
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise RequestError(
                ErrorCode.INVALID_FILTER,
                "The in operator requires between 1 and 100 scalar values.",
                details={"maximum": 100},
            )
        normalized_value: object = [_coerce_filter_value(item, spec, raw_alias) for item in value]
    else:
        if isinstance(value, list):
            raise RequestError(
                ErrorCode.INVALID_FILTER,
                "Only the in operator accepts an array value.",
                details={"field": raw_alias[:64]},
            )
        if operator in {"gte", "lte"} and value is None:
            raise RequestError(
                ErrorCode.INVALID_FILTER,
                "Ordered comparisons do not accept null values.",
                details={"field": raw_alias[:64]},
            )
        normalized_value = _coerce_filter_value(value, spec, raw_alias)

    return PreparedFilter(
        alias=cast(FieldAlias, raw_alias),
        operator=operator,
        value=normalized_value,
        spec=spec,
    )


def _coerce_filter_value(value: ScalarValue, spec: FieldSpec, alias: str) -> object:
    if value is None:
        if not spec.nullable:
            raise RequestError(
                ErrorCode.INVALID_FILTER,
                "Null is not allowed for this field.",
                details={"field": alias[:64]},
            )
        return None
    if spec.kind == "string":
        if not isinstance(value, str):
            raise _filter_type_error(alias)
        return value
    if spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _filter_type_error(alias)
        return value
    if spec.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _filter_type_error(alias)
        if isinstance(value, float) and not math.isfinite(value):
            raise _filter_type_error(alias)
        return Decimal(str(value))
    if spec.kind == "boolean":
        if not isinstance(value, bool):
            raise _filter_type_error(alias)
        return value
    if spec.kind == "date":
        if not isinstance(value, str):
            raise _filter_type_error(alias)
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _filter_type_error(alias) from exc
    if spec.kind == "uuid":
        if not isinstance(value, str):
            raise _filter_type_error(alias)
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise _filter_type_error(alias) from exc
    raise _filter_type_error(alias)


def _filter_type_error(alias: str) -> RequestError:
    return RequestError(
        ErrorCode.INVALID_FILTER,
        "Filter value does not match the field type.",
        details={"field": alias[:64]},
    )


def _request_fingerprint(
    request: FetchCustomerDataInput,
    entity: Entity,
    output_aliases: Sequence[FieldAlias],
) -> str:
    filters = [entry.model_dump(mode="json") for entry in request.filters or []]
    filters.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    sort_data = request.sort.model_dump(mode="json") if request.sort is not None else None
    canonical = {
        "entity": entity,
        "ids": sorted(request.ids or []),
        "field_aliases": sorted(output_aliases),
        "filters": filters,
        "sort": sort_data,
        "limit": request.limit,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_cursor(
    cursor: str,
    request: FetchCustomerDataInput,
    prepared: PreparedRequest,
) -> _CursorState:
    if not cursor or not _CURSOR_RE.fullmatch(cursor):
        raise RequestError(
            ErrorCode.INVALID_FILTER, "Cursor is invalid or does not match this query."
        )
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        state = _CursorState.model_validate(json.loads(decoded))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise RequestError(
            ErrorCode.INVALID_FILTER,
            "Cursor is invalid or does not match this query.",
        ) from exc

    expected_sort_alias = prepared.sort_alias
    expected_direction = prepared.sort_direction
    if (
        state.entity != prepared.entity
        or state.fingerprint != prepared.fingerprint
        or state.sort_field != expected_sort_alias
        or state.direction != expected_direction
        or not state.record_id
    ):
        raise RequestError(
            ErrorCode.INVALID_FILTER, "Cursor is invalid or does not match this query."
        )
    if len(state.record_id) > 200:
        raise RequestError(
            ErrorCode.INVALID_FILTER, "Cursor is invalid or does not match this query."
        )
    if request.sort is None and state.sort_value is not None:
        raise RequestError(
            ErrorCode.INVALID_FILTER, "Cursor is invalid or does not match this query."
        )
    return state


def _encode_cursor(
    prepared: PreparedRequest,
    record_id: object,
    sort_value: object,
) -> str:
    state = _CursorState(
        version=1,
        entity=prepared.entity,
        fingerprint=prepared.fingerprint,
        record_id=str(record_id),
        sort_field=prepared.sort_alias,
        direction=prepared.sort_direction,
        sort_value=_cursor_scalar(sort_value),
    )
    payload = json.dumps(state.to_wire(), sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_scalar(value: object) -> ScalarValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise RequestError(ErrorCode.INVALID_FILTER, "Cursor sort value is invalid.")
    if isinstance(value, (date, UUID, Decimal)):
        return str(value)
    raise RequestError(ErrorCode.INVALID_FILTER, "Cursor sort value is invalid.")


def _cursor_sort_value(state: _CursorState, sort_spec: FieldSpec) -> object:
    value = state.sort_value
    if value is None:
        return None
    try:
        if sort_spec.kind == "number":
            return Decimal(str(value))
        if sort_spec.kind == "integer":
            return int(value)
        if sort_spec.kind == "date":
            return date.fromisoformat(str(value))
        if sort_spec.kind == "uuid":
            return str(UUID(str(value)))
        if sort_spec.kind == "boolean":
            return bool(value)
        return str(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RequestError(
            ErrorCode.INVALID_FILTER, "Cursor is invalid or does not match this query."
        ) from exc


def _cursor_condition(
    prepared: PreparedRequest,
    state: _CursorState,
) -> ColumnElement[bool]:
    id_column = ENTITY_ID_COLUMNS[prepared.entity]
    record_id = _coerce_record_id(prepared.entity, state.record_id)
    if prepared.sort_alias is None or prepared.sort_spec is None:
        return id_column > record_id

    sort_column = prepared.sort_spec.expression
    sort_value = _cursor_sort_value(state, prepared.sort_spec)
    if sort_value is None:
        return and_(sort_column.is_(None), id_column > record_id)
    sort_after = (
        sort_column > sort_value if prepared.sort_direction == "asc" else sort_column < sort_value
    )
    return or_(
        sort_after, and_(sort_column == sort_value, id_column > record_id), sort_column.is_(None)
    )


def _coerce_record_id(entity: Entity, record_id: str) -> object:
    if entity == "predictions":
        try:
            return str(UUID(record_id))
        except ValueError as exc:
            raise RequestError(
                ErrorCode.INVALID_FILTER,
                "Cursor is invalid or does not match this query.",
            ) from exc
    return record_id


class DatabaseDataFetchClient:
    """Async SQLAlchemy implementation of the stable data-fetch protocol."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_fetch_rows: int,
    ) -> None:
        if not 1 <= max_fetch_rows <= 100:
            raise ValueError("max_fetch_rows must be between 1 and 100.")
        self._session_factory = session_factory
        self._max_fetch_rows = max_fetch_rows

    async def fetch_customer_data(self, request: FetchCustomerDataInput) -> FetchCustomerDataOutput:
        prepared = prepare_request(request, self._max_fetch_rows)
        cursor_state = (
            _decode_cursor(request.cursor, request, prepared)
            if request.cursor is not None
            else None
        )
        statement, sort_label = self._build_statement(request, prepared, cursor_state)
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(_READ_ONLY_TRANSACTION)
                result = await session.execute(statement)
                rows = result.mappings().all()
        except (SQLAlchemyError, TimeoutError, ConnectionError) as exc:
            logger.warning("Read-only data query failed", exc_info=True)
            raise DatabaseError(
                ErrorCode.DATABASE_UNAVAILABLE,
                "The data store is temporarily unavailable.",
                retryable=True,
            ) from exc

        requested_ids = set(request.ids or [])
        page_rows = [
            row for row in rows if request.ids is None or str(row["__record_id"]) in requested_ids
        ][: request.limit + 1]
        has_more = len(page_rows) > request.limit
        selected_rows = page_rows[: request.limit]
        records = [
            self._to_record(cast(Mapping[str, object], row), prepared) for row in selected_rows
        ]
        next_cursor = None
        if has_more and selected_rows:
            last_row = selected_rows[-1]
            last_sort_value = last_row.get(sort_label) if sort_label is not None else None
            next_cursor = _encode_cursor(
                prepared,
                last_row["__record_id"],
                last_sort_value,
            )
        return FetchCustomerDataOutput(records=records, next_cursor=next_cursor)

    def _build_statement(
        self,
        request: FetchCustomerDataInput,
        prepared: PreparedRequest,
        cursor_state: _CursorState | None,
    ) -> tuple[Select[object], str | None]:
        id_column = ENTITY_ID_COLUMNS[prepared.entity]
        selected = [
            FIELD_ALLOWLIST[prepared.entity][alias].expression.label(alias)
            for alias in prepared.output_aliases
        ]
        selected.append(id_column.label("__record_id"))

        sort_label: str | None = None
        if prepared.sort_spec is not None and prepared.sort_alias is not None:
            if prepared.sort_alias in prepared.output_aliases:
                sort_label = prepared.sort_alias
            else:
                sort_label = "__sort_value"
                selected.append(prepared.sort_spec.expression.label(sort_label))

        statement = select(*selected).select_from(ENTITY_TABLES[prepared.entity])
        if request.ids is not None:
            statement = statement.where(id_column.in_(request.ids))
        for prepared_filter in prepared.filters:
            statement = statement.where(_sql_filter(prepared_filter))
        if cursor_state is not None:
            statement = statement.where(_cursor_condition(prepared, cursor_state))

        if prepared.sort_spec is not None and prepared.sort_direction is not None:
            order = (
                prepared.sort_spec.expression.asc()
                if prepared.sort_direction == "asc"
                else prepared.sort_spec.expression.desc()
            )
            statement = statement.order_by(order.nulls_last(), id_column.asc())
        else:
            statement = statement.order_by(id_column.asc())
        statement = statement.limit(request.limit + 1)
        return statement, sort_label

    def _to_record(self, row: Mapping[str, object], prepared: PreparedRequest) -> FetchRecord:
        record_id = str(row["__record_id"])
        attributes: dict[FieldAlias, ScalarValue | dict[str, ScalarValue]] = {}
        for alias in prepared.output_aliases:
            value = row.get(alias)
            spec = FIELD_ALLOWLIST[prepared.entity][alias]
            if spec.kind == "feature_map":
                attributes[alias] = self._to_feature_map(value, prepared.entity, alias)
            else:
                attributes[alias] = _to_scalar(value)
        return FetchRecord(
            entity=prepared.entity,
            record_id=record_id,
            attributes=attributes,
        )

    @staticmethod
    def _to_feature_map(
        value: object,
        entity: Entity,
        alias: FieldAlias,
    ) -> dict[str, ScalarValue]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, ScalarValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not feature_value_is_allowed(entity, alias, raw_key):
                continue
            safe_scalar = raw_value is None or isinstance(raw_value, (str, int, bool))
            finite_number = isinstance(raw_value, float) and math.isfinite(raw_value)
            if safe_scalar or finite_number:
                result[raw_key] = raw_value
        return result


def _sql_filter(prepared_filter: PreparedFilter) -> ColumnElement[bool]:
    expression = prepared_filter.spec.expression
    operator = prepared_filter.operator
    value = prepared_filter.value
    if operator == "eq":
        return expression.is_(None) if value is None else expression == value
    if operator == "in":
        values = cast(list[object], value)
        non_null_values = [item for item in values if item is not None]
        include_null = len(non_null_values) != len(values)
        alternatives: list[ColumnElement[bool]] = []
        if non_null_values:
            alternatives.append(expression.in_(non_null_values))
        if include_null:
            alternatives.append(expression.is_(None))
        return or_(*alternatives)
    if operator == "gte":
        return expression >= value
    if operator == "lte":
        return expression <= value
    raise RequestError(ErrorCode.INVALID_FILTER, "Filter operator is not supported.")


def _to_scalar(value: object) -> ScalarValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise DatabaseError(
            ErrorCode.DATABASE_UNAVAILABLE,
            "The data store returned a value outside the supported result schema.",
            retryable=False,
        )
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, UUID)):
        return value.isoformat() if isinstance(value, date) else str(value)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    raise DatabaseError(
        ErrorCode.DATABASE_UNAVAILABLE,
        "The data store returned a value outside the supported result schema.",
        retryable=False,
    )
