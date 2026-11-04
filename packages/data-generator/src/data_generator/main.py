"""Finite dataset seed worker entry point."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable

from pydantic import Field, SecretStr, field_validator
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from common.logging import configure_logging
from common.settings import BaseSettings
from data_generator.datasets import DEFAULT_SOURCES, DatasetSnapshot, DatasetSource, load_snapshot
from data_generator.transform import (
    aggregate_churn_profiles,
    analyze_qoe_rows,
    assign_customer_plan_types,
    assign_customer_regions,
    build_customer_rows,
    choose_region_classes,
    collect_plan_type_counts,
    customers_by_region,
    iter_billing_cycle_rows,
    iter_qoe_sample_rows,
    iter_transaction_rows,
)
from data_generator.writer import DatabaseWriter, wait_for_database

_logger = logging.getLogger(__name__)
_CONNECT_TIMEOUT_SECONDS = 10
_POOL_TIMEOUT_SECONDS = 10
_STATEMENT_TIMEOUT_MILLISECONDS = 30_000
_POOL_SIZE = 2

type SnapshotLoader = Callable[[DatasetSource], DatasetSnapshot]


class DataGeneratorSettings(BaseSettings):
    database_url: SecretStr
    generator_seed: int = Field(default=42, ge=0)
    regional_qoe_churn_coupling: float = Field(default=0.4, ge=0.0, le=1.0)
    ingest_batch_size: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("database_url")
    @classmethod
    def require_async_postgres(cls, value: SecretStr) -> SecretStr:
        try:
            drivername = make_url(value.get_secret_value()).drivername
        except Exception as error:
            raise ValueError("DATABASE_URL must be a valid async PostgreSQL DSN") from error
        if drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg driver")
        return value


def create_engine(settings: DataGeneratorSettings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=_POOL_SIZE,
        max_overflow=0,
        pool_timeout=_POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        connect_args={
            "timeout": _CONNECT_TIMEOUT_SECONDS,
            "command_timeout": _STATEMENT_TIMEOUT_MILLISECONDS / 1000,
            "server_settings": {
                "statement_timeout": str(_STATEMENT_TIMEOUT_MILLISECONDS),
                "default_transaction_read_only": "off",
            },
        },
    )


async def run_generator(
    settings: DataGeneratorSettings,
    *,
    sources: tuple[DatasetSource, ...] = DEFAULT_SOURCES,
    snapshot_loader: SnapshotLoader = load_snapshot,
    engine_factory: Callable[[DataGeneratorSettings], AsyncEngine] = create_engine,
) -> dict[str, int]:
    """Load each configured source snapshot and idempotently upsert all source rows."""
    engine = engine_factory(settings)
    try:
        await wait_for_database(engine)
        snapshots: dict[str, DatasetSnapshot] = {}
        for source in sources:
            snapshots[source.name] = await asyncio.to_thread(snapshot_loader, source)
        if set(snapshots) != {"churn", "qoe", "ra_fraud"}:
            raise ValueError("the seed worker requires churn, qoe, and ra_fraud dataset sources")

        churn = snapshots["churn"]
        qoe = snapshots["qoe"]
        ra_fraud = snapshots["ra_fraud"]
        profiles = aggregate_churn_profiles(churn.rows)
        qoe_profile = analyze_qoe_rows(lambda: iter(qoe.rows), settings.generator_seed)
        plan_counts = collect_plan_type_counts(ra_fraud.rows)
        region_by_customer = assign_customer_regions(
            profiles,
            qoe_profile,
            settings.regional_qoe_churn_coupling,
            settings.generator_seed,
        )
        plan_by_customer = assign_customer_plan_types(
            profiles,
            plan_counts,
            settings.generator_seed,
        )
        customer_rows = build_customer_rows(
            profiles,
            region_by_customer,
            plan_by_customer,
            churn.source_revision,
        )
        by_plan: dict[str, list[str]] = defaultdict(list)
        for customer_id, plan_type in plan_by_customer.items():
            by_plan[plan_type].append(customer_id)
        region_members = customers_by_region(region_by_customer)
        region_classes = choose_region_classes(qoe_profile, settings.generator_seed)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        writer = DatabaseWriter(session_factory, settings.ingest_batch_size)
        counts = {
            "customers": await writer.upsert("customers", customer_rows),
            "billing_cycles": await writer.upsert(
                "billing_cycles",
                iter_billing_cycle_rows(
                    churn.rows,
                    churn.source,
                    churn.source_revision,
                ),
            ),
            "transactions": await writer.upsert(
                "transactions",
                iter_transaction_rows(
                    ra_fraud.rows,
                    ra_fraud.source,
                    ra_fraud.source_revision,
                    plan_counts,
                    by_plan,
                    settings.generator_seed,
                ),
            ),
            "qoe_samples": await writer.upsert(
                "qoe_samples",
                iter_qoe_sample_rows(
                    qoe.rows,
                    qoe.source,
                    qoe.source_revision,
                    qoe_profile,
                    region_members,
                    region_classes,
                    settings.generator_seed,
                ),
            ),
        }
        _logger.info(
            "dataset seed completed",
            extra={"component": "data-generator", "row_counts": counts},
        )
        return counts
    finally:
        await engine.dispose()


async def _run_from_environment() -> None:
    configure_logging()
    settings = DataGeneratorSettings()
    await run_generator(settings)


def main() -> None:
    asyncio.run(_run_from_environment())


if __name__ == "__main__":
    main()
