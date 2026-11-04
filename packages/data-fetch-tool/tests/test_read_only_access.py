"""Database query allowlist, binding, and per-call session checks."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import cast

import pytest
from data_fetch_tool.repository import DatabaseDataFetchClient
from data_fetch_tool.schemas import FetchCustomerDataInput
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ClauseElement

from common.errors import DatabaseError, RequestError


def _postgresql_dialect() -> Dialect:
    return cast(Dialect, postgresql.dialect())  # type: ignore[no-untyped-call]


class ResultStub:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._rows = list(rows)

    def mappings(self) -> ResultStub:
        return self

    def all(self) -> list[Mapping[str, object]]:
        return self._rows


class TransactionStub:
    async def __aenter__(self) -> TransactionStub:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class SessionStub:
    def __init__(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        fail_on_select: bool = False,
    ) -> None:
        self.rows = list(rows)
        self.fail_on_select = fail_on_select
        self.statements: list[object] = []
        self.closed = False

    async def __aenter__(self) -> SessionStub:
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        self.closed = True
        return None

    def begin(self) -> TransactionStub:
        return TransactionStub()

    async def execute(self, statement: object) -> ResultStub:
        self.statements.append(statement)
        if str(statement).lstrip().upper().startswith("SELECT") and self.fail_on_select:
            raise SQLAlchemyError("private driver and query details")
        return ResultStub(self.rows if str(statement).lstrip().upper().startswith("SELECT") else [])


class SessionFactoryStub:
    def __init__(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        session_count: int = 1,
        fail_on_select_for: frozenset[int] = frozenset(),
    ) -> None:
        self.rows = list(rows)
        self.session_count = session_count
        self.fail_on_select_for = fail_on_select_for
        self.sessions: list[SessionStub] = []

    def __call__(self) -> SessionStub:
        index = len(self.sessions)
        if index >= self.session_count:
            raise AssertionError("Unexpected additional database session.")
        session = SessionStub(self.rows, fail_on_select=index in self.fail_on_select_for)
        self.sessions.append(session)
        return session


def _client(factory: SessionFactoryStub) -> DatabaseDataFetchClient:
    session_factory = cast(async_sessionmaker[AsyncSession], factory)
    return DatabaseDataFetchClient(session_factory, max_fetch_rows=10)


def _request(
    *, ids: list[str] | None = None, aliases: list[str] | None = None
) -> FetchCustomerDataInput:
    return FetchCustomerDataInput(
        entity="customers",
        ids=ids or ["customer-1"],
        field_aliases=aliases or ["customer_id", "dollar_weight"],
        limit=10,
    )


@pytest.mark.asyncio
async def test_query_is_read_only_bound_and_returns_only_requested_entity_ids() -> None:
    factory = SessionFactoryStub(
        [
            {
                "customer_id": "customer-1",
                "dollar_weight": 40.5,
                "__record_id": "customer-1",
            },
            {
                "customer_id": "outside-request",
                "dollar_weight": 75.0,
                "__record_id": "outside-request",
            },
        ]
    )
    output = await _client(factory).fetch_customer_data(_request())

    assert len(factory.sessions) == 1
    session = factory.sessions[0]
    assert session.closed
    assert str(session.statements[0]).upper() == "SET TRANSACTION READ ONLY"
    select_statement = cast(ClauseElement, session.statements[1])
    compiled = select_statement.compile(dialect=_postgresql_dialect())
    sql = str(compiled)
    assert "FROM app.customers" in sql
    assert "customer-1" not in sql
    assert any(
        value == "customer-1" or value == ["customer-1"] for value in compiled.params.values()
    )
    assert [record.record_id for record in output.records] == ["customer-1"]
    assert all(record.entity == "customers" for record in output.records)


@pytest.mark.asyncio
async def test_unknown_field_alias_is_rejected_before_opening_a_session() -> None:
    factory = SessionFactoryStub([])
    request = FetchCustomerDataInput(
        entity="customers",
        ids=["customer-1"],
        field_aliases=["internal_sql_column"],
        limit=1,
    )

    with pytest.raises(RequestError, match="Field alias"):
        await _client(factory).fetch_customer_data(request)

    assert factory.sessions == []


@pytest.mark.asyncio
async def test_parallel_calls_get_distinct_sessions_that_close_on_success() -> None:
    rows = [
        {
            "customer_id": "customer-1",
            "dollar_weight": 40.5,
            "__record_id": "customer-1",
        }
    ]
    factory = SessionFactoryStub(rows, session_count=2)
    client = _client(factory)

    first, second = await asyncio.gather(
        client.fetch_customer_data(_request()),
        client.fetch_customer_data(_request()),
    )

    assert first.to_wire() == second.to_wire()
    assert len(factory.sessions) == 2
    assert factory.sessions[0] is not factory.sessions[1]
    assert all(session.closed for session in factory.sessions)


@pytest.mark.asyncio
async def test_database_failure_closes_session_and_returns_safe_error() -> None:
    factory = SessionFactoryStub(
        [],
        fail_on_select_for=frozenset({0}),
    )

    with pytest.raises(DatabaseError) as captured:
        await _client(factory).fetch_customer_data(_request())

    assert factory.sessions[0].closed
    assert captured.value.message == "The data store is temporarily unavailable."
    assert "private" not in captured.value.message
