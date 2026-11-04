"""In-memory implementation of the consumer-facing data-fetch protocol."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from functools import cmp_to_key
from typing import cast

from data_fetch_tool.field_allowlist import FIELD_ALLOWLIST
from data_fetch_tool.repository import (
    PreparedFilter,
    PreparedRequest,
    _cursor_sort_value,
    _CursorState,
    _decode_cursor,
    _encode_cursor,
    prepare_request,
)
from data_fetch_tool.schemas import (
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
    FetchRecord,
    FieldAlias,
    ScalarValue,
)

ComparableValue = str | int | bool | Decimal | date


class InMemoryDataFetchClient:
    """Small deterministic fake with the same input and output contract as the DB client."""

    def __init__(self, records: Sequence[FetchRecord], max_fetch_rows: int = 100) -> None:
        if not 1 <= max_fetch_rows <= 100:
            raise ValueError("max_fetch_rows must be between 1 and 100.")
        self._records = tuple(records)
        self._max_fetch_rows = max_fetch_rows

    async def fetch_customer_data(self, request: FetchCustomerDataInput) -> FetchCustomerDataOutput:
        prepared = prepare_request(request, self._max_fetch_rows)
        cursor_state = (
            _decode_cursor(request.cursor, request, prepared)
            if request.cursor is not None
            else None
        )
        selected = [
            record
            for record in self._records
            if record.entity == prepared.entity and self._matches(record, request, prepared)
        ]
        selected = self._sort_records(selected, prepared)
        if cursor_state is not None:
            selected = [
                record for record in selected if self._after_cursor(record, prepared, cursor_state)
            ]
        page = selected[: request.limit + 1]
        has_more = len(page) > request.limit
        records = [self._project(record, prepared) for record in page[: request.limit]]
        next_cursor = None
        if has_more and records:
            last_source = page[request.limit - 1]
            sort_value = (
                last_source.attributes.get(prepared.sort_alias)
                if prepared.sort_alias is not None
                else None
            )
            next_cursor = _encode_cursor(prepared, last_source.record_id, sort_value)
        return FetchCustomerDataOutput(records=records, next_cursor=next_cursor)

    @staticmethod
    def _matches(
        record: FetchRecord,
        request: FetchCustomerDataInput,
        prepared: PreparedRequest,
    ) -> bool:
        if request.ids is not None and record.record_id not in request.ids:
            return False
        return all(_matches_filter(record, item) for item in prepared.filters)

    @staticmethod
    def _sort_records(records: list[FetchRecord], prepared: PreparedRequest) -> list[FetchRecord]:
        records.sort(key=lambda record: record.record_id)
        if prepared.sort_alias is None or prepared.sort_spec is None:
            return records
        populated: list[tuple[ComparableValue, FetchRecord]] = []
        missing: list[FetchRecord] = []
        for record in records:
            value = _typed_value(
                record.attributes.get(prepared.sort_alias),
                prepared.sort_spec.kind,
            )
            if value is None:
                missing.append(record)
            else:
                populated.append((value, record))

        def compare(
            left: tuple[ComparableValue, FetchRecord],
            right: tuple[ComparableValue, FetchRecord],
        ) -> int:
            comparison = _compare_values(left[0], right[0])
            return comparison if prepared.sort_direction == "asc" else -comparison

        populated.sort(key=cmp_to_key(compare))
        return [record for _, record in populated] + missing

    @staticmethod
    def _after_cursor(
        record: FetchRecord,
        prepared: PreparedRequest,
        state: _CursorState,
    ) -> bool:
        if prepared.sort_alias is None or prepared.sort_spec is None:
            return record.record_id > state.record_id
        current = _typed_value(
            record.attributes.get(prepared.sort_alias),
            prepared.sort_spec.kind,
        )
        last = _typed_value(
            _cursor_sort_value(state, prepared.sort_spec),
            prepared.sort_spec.kind,
        )
        if last is None:
            return current is None and record.record_id > state.record_id
        if current is None:
            return True
        comparison = _compare_values(current, last)
        if comparison == 0:
            return record.record_id > state.record_id
        return comparison > 0 if prepared.sort_direction == "asc" else comparison < 0

    @staticmethod
    def _project(record: FetchRecord, prepared: PreparedRequest) -> FetchRecord:
        attributes: dict[FieldAlias, ScalarValue | dict[str, ScalarValue]] = {}
        for alias in prepared.output_aliases:
            value = record.attributes.get(alias)
            spec = FIELD_ALLOWLIST[prepared.entity][alias]
            if spec.kind == "feature_map":
                attributes[alias] = value if isinstance(value, dict) else {}
            else:
                attributes[alias] = cast(ScalarValue, value)
        return FetchRecord(
            entity=prepared.entity,
            record_id=record.record_id,
            attributes=attributes,
        )


def _matches_filter(record: FetchRecord, prepared_filter: PreparedFilter) -> bool:
    actual = record.attributes.get(prepared_filter.alias)
    values = prepared_filter.value
    if prepared_filter.operator == "in":
        return any(
            _equal_value(actual, value, prepared_filter) for value in cast(list[object], values)
        )
    if prepared_filter.operator == "eq":
        return _equal_value(actual, values, prepared_filter)
    comparable = _typed_value(actual, prepared_filter.spec.kind)
    expected = _typed_value(values, prepared_filter.spec.kind)
    if comparable is None or expected is None:
        return False
    comparison = _compare_values(comparable, expected)
    if prepared_filter.operator == "gte":
        return comparison >= 0
    if prepared_filter.operator == "lte":
        return comparison <= 0
    return False


def _equal_value(actual: object, expected: object, prepared_filter: PreparedFilter) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return _typed_value(actual, prepared_filter.spec.kind) == expected


def _typed_value(value: object, kind: str) -> ComparableValue | None:
    if value is None:
        return None
    if kind == "number":
        return Decimal(str(value))
    if kind == "integer":
        return int(cast(int, value))
    if kind == "date":
        return value if isinstance(value, date) else date.fromisoformat(cast(str, value))
    if kind == "boolean":
        return cast(bool, value)
    return cast(str, value)


def _compare_values(left: ComparableValue, right: ComparableValue) -> int:
    if isinstance(left, bool) and isinstance(right, bool):
        return (left > right) - (left < right)
    if isinstance(left, int) and isinstance(right, int):
        return (left > right) - (left < right)
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return (left > right) - (left < right)
    if isinstance(left, date) and isinstance(right, date):
        return (left > right) - (left < right)
    raise TypeError("Fake query values do not match the field type.")
