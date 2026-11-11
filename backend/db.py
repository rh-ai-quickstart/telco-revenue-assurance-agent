import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _async_url(raw: str) -> str:
    for prefix in ("postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix):]
    return raw


_DATABASE_URL = _async_url(os.environ["DATABASE_URL"])

engine = create_async_engine(_DATABASE_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
