"""Read-only DSN validation and bounded async PostgreSQL engine options."""

from typing import cast

import data_fetch_tool.repository as repository
import pytest
from data_fetch_tool.main import load_service_settings
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine


def _set_non_dsn_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_HOST", "10.20.30.40")
    monkeypatch.setenv("MCP_PORT", "8000")
    monkeypatch.setenv("MAX_FETCH_ROWS", "100")


def test_missing_readonly_dsn_fails_startup_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_non_dsn_settings(monkeypatch)
    monkeypatch.delenv("DATABASE_READONLY_URL", raising=False)

    with pytest.raises(ValidationError) as captured:
        load_service_settings()

    assert "database_readonly_url" in str(captured.value)


def test_non_postgresql_dsn_is_rejected_without_a_fallback() -> None:
    with pytest.raises(ValueError, match=r"postgresql\+asyncpg"):
        repository.create_readonly_engine(SecretStr("sqlite+aiosqlite:///unused.db"))


def test_malformed_dsn_fails_with_a_safe_configuration_message() -> None:
    with pytest.raises(ValueError, match="DATABASE_READONLY_URL must be a valid"):
        repository.create_readonly_engine(SecretStr("not a database URI"))


def test_engine_uses_finite_timeouts_and_readonly_postgresql_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    marker = cast(AsyncEngine, object())

    def capture_engine(url: URL, **options: object) -> AsyncEngine:
        captured["url"] = url
        captured.update(options)
        return marker

    monkeypatch.setattr(repository, "create_async_engine", capture_engine)
    engine = repository.create_readonly_engine(
        SecretStr("postgresql+asyncpg://telco_reader@database.internal/telco_revenue_assurance")
    )

    assert engine is marker
    assert cast(URL, captured["url"]).drivername == "postgresql+asyncpg"
    connect_args = cast(dict[str, object], captured["connect_args"])
    assert float(cast(int | float, connect_args["timeout"])) > 0
    assert float(cast(int | float, connect_args["command_timeout"])) > 0
    server_settings = cast(dict[str, str], connect_args["server_settings"])
    assert server_settings["default_transaction_read_only"] == "on"
    assert int(server_settings["statement_timeout"]) > 0
    assert cast(int, captured["pool_size"]) > 0
    assert float(cast(int | float, captured["pool_timeout"])) > 0
