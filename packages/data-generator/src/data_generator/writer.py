"""Bounded PostgreSQL upserts for the four seed-owned application tables."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Literal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

type DatabaseRow = Mapping[str, object]
type TableName = Literal["customers", "billing_cycles", "transactions", "qoe_samples"]

_METADATA = MetaData(schema="app")
TABLES: dict[TableName, Table] = {
    "customers": Table(
        "customers",
        _METADATA,
        Column("customer_id", Text, primary_key=True),
        Column("plan_type", Text, nullable=True),
        Column("dollar_weight", Numeric(18, 6), nullable=False),
        Column("region_id", Text, nullable=False),
        Column("source_dataset_revision", Text, nullable=False),
        Column("source_features", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    ),
    "billing_cycles": Table(
        "billing_cycles",
        _METADATA,
        Column("billing_cycle_id", Text, primary_key=True),
        Column("customer_id", Text, nullable=False),
        Column("billing_cycle_start", Date, nullable=False),
        Column("total_bill_amount", Numeric(18, 6), nullable=False),
        Column("source_dataset_revision", Text, nullable=False),
        Column("source_features", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    ),
    "transactions": Table(
        "transactions",
        _METADATA,
        Column("transaction_id", Text, primary_key=True),
        Column("customer_id", Text, nullable=False),
        Column("plan_type", Text, nullable=False),
        Column("is_fraud", Boolean, nullable=False),
        Column("source_dataset_revision", Text, nullable=False),
        Column("source_features", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    ),
    "qoe_samples": Table(
        "qoe_samples",
        _METADATA,
        Column("qoe_sample_id", Text, primary_key=True),
        Column("customer_id", Text, nullable=False),
        Column("region_id", Text, nullable=False),
        Column("latitude", Float, nullable=False),
        Column("longitude", Float, nullable=False),
        Column("elevation_m", Float, nullable=False),
        Column("visible_satellites", Integer, nullable=False),
        Column("serving_satellites", Integer, nullable=False),
        Column("signal_loss_db", Float, nullable=False),
        Column("season", Text, nullable=False),
        Column("weather", Text, nullable=False),
        Column("source_dataset_revision", Text, nullable=False),
        Column("source_features", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    ),
}
PRIMARY_KEYS: dict[TableName, str] = {
    "customers": "customer_id",
    "billing_cycles": "billing_cycle_id",
    "transactions": "transaction_id",
    "qoe_samples": "qoe_sample_id",
}

_MAX_WRITE_RETRIES = 3
_MAX_RETRY_DELAY_SECONDS = 2.0


class DatabaseWriteError(RuntimeError):
    """Safe failure raised after a batch cannot be committed."""


def build_upsert_statement(table_name: TableName, rows: Sequence[DatabaseRow]) -> Insert:
    if not rows:
        raise ValueError("an upsert batch must contain at least one row")
    table = TABLES[table_name]
    allowed_columns = set(table.c.keys())
    expected_columns = set(rows[0])
    if not expected_columns <= allowed_columns:
        unexpected = sorted(expected_columns - allowed_columns)
        raise ValueError(f"unrecognized columns for {table_name}: {', '.join(unexpected)}")
    if any(set(row) != expected_columns for row in rows):
        raise ValueError("all rows in one upsert batch must have the same columns")
    primary_key = PRIMARY_KEYS[table_name]
    if primary_key not in expected_columns:
        raise ValueError(f"upsert rows for {table_name} must include {primary_key}")

    statement = postgresql_insert(table).values([dict(row) for row in rows])
    update_values = {
        column_name: getattr(statement.excluded, column_name)
        for column_name in expected_columns
        if column_name != primary_key
    }
    if table_name == "customers":
        update_values["updated_at"] = func.now()
    if update_values:
        return statement.on_conflict_do_update(
            index_elements=[table.c[primary_key]],
            set_=update_values,
        )
    return statement.on_conflict_do_nothing(index_elements=[table.c[primary_key]])


class DatabaseWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        batch_size: int,
        *,
        retry_limit: int = _MAX_WRITE_RETRIES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("ingest batch size must be between 1 and 10000")
        if retry_limit < 0 or retry_limit > 5:
            raise ValueError("write retry limit must be between 0 and 5")
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._retry_limit = retry_limit
        self._sleep = sleep

    async def upsert(self, table_name: TableName, rows: Iterable[DatabaseRow]) -> int:
        total = 0
        batch: list[DatabaseRow] = []
        for row in rows:
            batch.append(row)
            if len(batch) == self._batch_size:
                await self._write_batch(table_name, batch)
                total += len(batch)
                batch.clear()
        if batch:
            await self._write_batch(table_name, batch)
            total += len(batch)
        return total

    async def _write_batch(self, table_name: TableName, rows: Sequence[DatabaseRow]) -> None:
        statement = build_upsert_statement(table_name, rows)
        for attempt in range(self._retry_limit + 1):
            try:
                async with self._session_factory() as session, session.begin():
                    await session.execute(statement)
                return
            except SQLAlchemyError as error:
                if attempt >= self._retry_limit or not _is_retryable_database_error(error):
                    raise DatabaseWriteError(
                        f"database upsert failed for {table_name} after {attempt + 1} attempt(s)"
                    ) from None
                delay = min(0.25 * (2**attempt), _MAX_RETRY_DELAY_SECONDS)
                await self._sleep(delay)


def _is_retryable_database_error(error: SQLAlchemyError) -> bool:
    original = getattr(error, "orig", error)
    sqlstate = getattr(original, "sqlstate", None)
    if not isinstance(sqlstate, str):
        return False
    return sqlstate in {"40001", "40P01", "57P01"} or sqlstate.startswith("08")


async def wait_for_database(
    engine: AsyncEngine,
    *,
    readiness_deadline_seconds: float = 60.0,
    attempt_limit: int = 8,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Probe PostgreSQL with a finite deadline and retry cap before loading data."""
    if readiness_deadline_seconds <= 0 or attempt_limit < 1:
        raise ValueError("database readiness deadline and attempt limit must be positive")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + readiness_deadline_seconds
    for attempt in range(attempt_limit):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            return
        except (SQLAlchemyError, TimeoutError):
            pass
        remaining = deadline - loop.time()
        if attempt + 1 >= attempt_limit or remaining <= 0:
            break
        delay = min(0.25 * (2**attempt), 4.0, remaining)
        await sleep(delay)
    raise DatabaseWriteError(
        "database did not become ready within the configured deadline"
    ) from None
