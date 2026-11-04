from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import cast

from data_generator.datasets import DatasetSource
from data_generator.transform import (
    DatabaseRow,
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
from data_generator.writer import PRIMARY_KEYS, TableName, build_upsert_statement
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Dialect

_CHURN_SOURCE = DatasetSource("churn", "Omer1234567/telco-churn-synthetic", "a" * 40)
_QOE_SOURCE = DatasetSource("qoe", "fenar/starlink", "b" * 40)
_RAFM_SOURCE = DatasetSource("ra_fraud", "fenar/revenue_assurance", "c" * 40)
_SOURCE_REVISIONS = {
    "churn": "Omer1234567/telco-churn-synthetic@" + "a" * 40 + ":train",
    "qoe": "fenar/starlink@" + "b" * 40 + ":train",
    "ra_fraud": "fenar/revenue_assurance@" + "c" * 40 + ":train",
}
_postgresql_dialect_factory = cast(Callable[[], Dialect], PGDialect)


def _fixture_rows() -> dict[TableName, list[DatabaseRow]]:
    churn_rows: list[dict[str, object]] = [
        {
            "CustomerID": "customer-a",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 100.0,
            "HasChurned": False,
        },
        {
            "CustomerID": "customer-b",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 80.0,
            "HasChurned": True,
        },
    ]
    qoe_rows: list[dict[str, object]] = [
        {
            "latitude": 1.0,
            "longitude": 1.0,
            "elevation_m": 30.0,
            "visible_satellites": 10,
            "serving_satellites": 8,
            "signal_loss_db": 12.0,
            "season": "winter",
            "weather": "rain",
            "quality_label": "poor",
        },
        {
            "latitude": 46.0,
            "longitude": 46.0,
            "elevation_m": 5.0,
            "visible_satellites": 20,
            "serving_satellites": 18,
            "signal_loss_db": 1.0,
            "season": "summer",
            "weather": "clear",
            "quality_label": "good",
        },
    ]
    transaction_rows: list[dict[str, object]] = [
        {"Plan_Type": "Basic", "Fraud": 0, "transaction_value": 15.0},
        {"Plan_Type": "Premium", "Fraud": 1, "transaction_value": 90.0},
        {"Plan_Type": "Basic", "Fraud": 0, "transaction_value": 25.0},
    ]

    profiles = aggregate_churn_profiles(churn_rows)
    qoe_profile = analyze_qoe_rows(lambda: iter(qoe_rows), seed=42)
    plan_counts = collect_plan_type_counts(transaction_rows)
    region_by_customer = assign_customer_regions(profiles, qoe_profile, coupling=0.4, seed=42)
    plan_by_customer = assign_customer_plan_types(profiles, plan_counts, seed=42)
    customer_rows = build_customer_rows(
        profiles,
        region_by_customer,
        plan_by_customer,
        _SOURCE_REVISIONS["churn"],
    )
    customers_for_plan: dict[str, list[str]] = defaultdict(list)
    for customer_id, plan_type in plan_by_customer.items():
        customers_for_plan[plan_type].append(customer_id)
    region_members = customers_by_region(region_by_customer)

    return {
        "customers": customer_rows,
        "billing_cycles": list(
            iter_billing_cycle_rows(churn_rows, _CHURN_SOURCE, _SOURCE_REVISIONS["churn"])
        ),
        "transactions": list(
            iter_transaction_rows(
                transaction_rows,
                _RAFM_SOURCE,
                _SOURCE_REVISIONS["ra_fraud"],
                plan_counts,
                customers_for_plan,
                seed=42,
            )
        ),
        "qoe_samples": list(
            iter_qoe_sample_rows(
                qoe_rows,
                _QOE_SOURCE,
                _SOURCE_REVISIONS["qoe"],
                qoe_profile,
                region_members,
                choose_region_classes(qoe_profile, seed=42),
                seed=42,
            )
        ),
    }


def _upsert_batches(
    store: dict[TableName, dict[str, DatabaseRow]],
    table_name: TableName,
    rows: list[DatabaseRow],
    *,
    batch_size: int,
    batch_limit: int | None = None,
) -> int:
    primary_key = PRIMARY_KEYS[table_name]
    processed_rows = 0
    for batch_number, offset in enumerate(range(0, len(rows), batch_size), start=1):
        batch = rows[offset : offset + batch_size]
        statement = build_upsert_statement(table_name, batch)
        compiled = str(statement.compile(dialect=_postgresql_dialect_factory())).lower()
        assert "on conflict" in compiled
        assert "do update set" in compiled
        for row in batch:
            key = str(row[primary_key])
            store[table_name][key] = dict(row)
        processed_rows += len(batch)
        if batch_limit is not None and batch_number >= batch_limit:
            break
    return processed_rows


def _apply_load(
    store: dict[TableName, dict[str, DatabaseRow]],
    rows_by_table: dict[TableName, list[DatabaseRow]],
    *,
    interrupted: bool = False,
) -> None:
    for table_name, rows in rows_by_table.items():
        _upsert_batches(
            store,
            table_name,
            rows,
            batch_size=1,
            batch_limit=1 if interrupted else None,
        )


def test_repeated_and_resumed_snapshot_loads_keep_stable_row_counts() -> None:
    first_snapshot = _fixture_rows()
    same_snapshot = _fixture_rows()
    assert same_snapshot == first_snapshot

    store: dict[TableName, dict[str, DatabaseRow]] = {
        table_name: {} for table_name in first_snapshot
    }
    _apply_load(store, first_snapshot, interrupted=True)
    _apply_load(store, same_snapshot)
    resumed_counts = {table_name: len(rows) for table_name, rows in store.items()}

    _apply_load(store, _fixture_rows())
    repeated_counts = {table_name: len(rows) for table_name, rows in store.items()}

    expected_counts = {table_name: len(rows) for table_name, rows in first_snapshot.items()}
    assert resumed_counts == expected_counts
    assert repeated_counts == expected_counts
    for table_name, rows in first_snapshot.items():
        expected = {str(row[PRIMARY_KEYS[table_name]]): row for row in rows}
        assert store[table_name] == expected
