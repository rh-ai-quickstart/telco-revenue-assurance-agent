from __future__ import annotations

from collections.abc import Callable

import pytest
from api.db.session import ApiSettings, async_database_url, sync_database_url
from api.main import create_app
from pydantic import ValidationError


def test_async_and_sync_dsn_conversion_never_double_prefixes_driver() -> None:
    async_dsn = "postgresql+asyncpg://api@db.example.test:5432/telco"
    sync_dsn = "postgresql+psycopg://api@db.example.test:5432/telco"

    assert async_database_url(async_dsn) == async_dsn
    assert sync_database_url(async_dsn) == sync_dsn
    assert async_database_url(sync_dsn) == async_dsn
    assert sync_database_url(sync_dsn) == sync_dsn
    assert async_database_url("postgresql://api@db.example.test/telco") == (
        "postgresql+asyncpg://api@db.example.test/telco"
    )


@pytest.mark.parametrize(
    "converter",
    [async_database_url, sync_database_url],
)
def test_database_url_rejects_non_postgresql_schemes(
    converter: Callable[[str], str],
) -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        converter("sqlite:///temporary.db")


@pytest.mark.asyncio
async def test_missing_required_settings_fail_at_startup_not_module_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DATABASE_URL",
        "OGX_BASE_URL",
        "OGX_MODEL_ID",
        "PREDICTION_MCP_URL",
        "DATA_FETCH_MCP_URL",
        "SCORING_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)

    application = create_app()
    with pytest.raises(ValidationError) as caught:
        async with application.router.lifespan_context(application):
            pass

    assert "database_url" in str(caught.value)
    assert "ogx_model_id" in str(caught.value)


def test_complete_settings_accept_postgresql_and_bounded_batch_size() -> None:
    settings = ApiSettings(
        database_url="postgresql://api@db.example.test/telco",
        ogx_base_url="http://ogx.example.test:8321",
        ogx_model_id="llama-3.1-8b-instruct",
        prediction_mcp_url="http://prediction.example.test:8000/mcp",
        data_fetch_mcp_url="http://fetch.example.test:8000/mcp",
        scoring_batch_size=100,
    )

    assert settings.scoring_batch_size == 100
    with pytest.raises(ValidationError):
        ApiSettings(
            database_url="postgresql://api@db.example.test/telco",
            ogx_base_url="http://ogx.example.test:8321",
            ogx_model_id="llama-3.1-8b-instruct",
            prediction_mcp_url="http://prediction.example.test:8000/mcp",
            data_fetch_mcp_url="http://fetch.example.test:8000/mcp",
            scoring_batch_size=101,
        )
