from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest
from data_generator.main import DataGeneratorSettings
from data_generator.writer import DatabaseWriteError, wait_for_database
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine


def test_settings_use_documented_defaults_and_require_async_postgres() -> None:
    settings = DataGeneratorSettings(
        database_url=SecretStr("postgresql+asyncpg://seed:secret@localhost:5432/telco")
    )

    assert settings.generator_seed == 42
    assert settings.regional_qoe_churn_coupling == 0.4
    assert settings.ingest_batch_size == 1000

    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg"):
        DataGeneratorSettings(database_url=SecretStr("sqlite+aiosqlite:///local.db"))


def test_settings_bound_coupling_and_ingest_batch_size() -> None:
    database_url = SecretStr("postgresql+asyncpg://seed:secret@localhost:5432/telco")

    with pytest.raises(ValidationError):
        DataGeneratorSettings(database_url=database_url, regional_qoe_churn_coupling=1.1)
    with pytest.raises(ValidationError):
        DataGeneratorSettings(database_url=database_url, ingest_batch_size=10_001)


class _FakeConnection:
    def __init__(self, should_fail: bool) -> None:
        self._should_fail = should_fail

    async def execute(self, statement: object) -> None:
        assert str(statement) == "SELECT 1"
        if self._should_fail:
            raise SQLAlchemyError("database is not ready")


class _FakeEngine:
    def __init__(self, failed_connections: int) -> None:
        self.failed_connections = failed_connections
        self.attempts = 0

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_FakeConnection]:
        self.attempts += 1
        yield _FakeConnection(self.attempts <= self.failed_connections)


def test_readiness_succeeds_after_bounded_retries() -> None:
    engine = _FakeEngine(failed_connections=2)
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    asyncio.run(
        wait_for_database(
            cast(AsyncEngine, engine),
            readiness_deadline_seconds=5,
            attempt_limit=4,
            sleep=no_sleep,
        )
    )

    assert engine.attempts == 3
    assert delays == [0.25, 0.5]


def test_readiness_stops_after_its_attempt_limit() -> None:
    engine = _FakeEngine(failed_connections=10)
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(DatabaseWriteError, match="configured deadline"):
        asyncio.run(
            wait_for_database(
                cast(AsyncEngine, engine),
                readiness_deadline_seconds=5,
                attempt_limit=3,
                sleep=no_sleep,
            )
        )

    assert engine.attempts == 3
    assert delays == [0.25, 0.5]
