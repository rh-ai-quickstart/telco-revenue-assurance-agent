"""Environment-backed service settings and PostgreSQL session helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import AnyHttpUrl, Field
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from common.settings import BaseSettings


class ApiSettings(BaseSettings):
    """Validated runtime settings loaded only when the application starts."""

    database_url: str
    ogx_base_url: AnyHttpUrl
    ogx_model_id: str = Field(min_length=1)
    prediction_mcp_url: AnyHttpUrl
    data_fetch_mcp_url: AnyHttpUrl
    scoring_batch_size: int = Field(ge=1, le=100)


def _postgres_url(dsn: str) -> URL:
    try:
        url = make_url(dsn)
    except Exception as exc:
        raise ValueError("DATABASE_URL must be a valid PostgreSQL DSN.") from exc
    if url.get_backend_name() not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL must use PostgreSQL.")
    return url


def async_database_url(dsn: str) -> str:
    """Return a PostgreSQL asyncpg DSN without double-prefixing its driver."""

    url = _postgres_url(dsn)
    return url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


def sync_database_url(dsn: str) -> str:
    """Return a PostgreSQL psycopg DSN for Alembic's synchronous migration path."""

    url = _postgres_url(dsn)
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def create_database_engine(settings: ApiSettings) -> AsyncEngine:
    return create_async_engine(async_database_url(settings.database_url), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
