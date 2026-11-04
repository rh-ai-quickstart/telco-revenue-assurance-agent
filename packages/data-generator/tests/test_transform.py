from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import cast

from data_generator.datasets import DatasetSource, normalize_raw_features
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
    stable_source_key,
)

_CHURN_SOURCE = DatasetSource("churn", "Omer1234567/telco-churn-synthetic", "a" * 40)
_QOE_SOURCE = DatasetSource("qoe", "fenar/starlink", "b" * 40)
_RAFM_SOURCE = DatasetSource("ra_fraud", "fenar/revenue_assurance", "c" * 40)


def _source_features(row: dict[str, object]) -> Mapping[str, object]:
    return cast(Mapping[str, object], row["source_features"])


def churn_rows() -> list[dict[str, object]]:
    return [
        {
            "CustomerID": "customer-a",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 100.0,
            "HasChurned": False,
            "PaymentStatus": "Current",
        },
        {
            "CustomerID": "customer-a",
            "BillingCycleStart": "2024-02-01",
            "TotalBillAmount": 200.0,
            "HasChurned": True,
            "PaymentStatus": "Late",
        },
        {
            "CustomerID": "customer-b",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 60.0,
            "HasChurned": False,
            "PaymentStatus": "Current",
        },
        {
            "CustomerID": "customer-c",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 80.0,
            "HasChurned": True,
            "PaymentStatus": "Late",
        },
        {
            "CustomerID": "customer-d",
            "BillingCycleStart": "2024-01-01",
            "TotalBillAmount": 90.0,
            "HasChurned": False,
            "PaymentStatus": "Current",
        },
    ]


def qoe_rows() -> list[dict[str, object]]:
    return [
        {
            "latitude": 1.0,
            "longitude": 1.0,
            "elevation_m": 30.0,
            "visible_satellites": 10,
            "serving_satellites": 8,
            "signal_loss_db": 12.0,
            "season": "winter",
            "weather": "rain",
            "qoe_class": "poor",
        },
        {
            "latitude": 2.0,
            "longitude": 2.0,
            "elevation_m": 35.0,
            "visible_satellites": 11,
            "serving_satellites": 7,
            "signal_loss_db": 10.0,
            "season": "winter",
            "weather": "cloudy",
            "qoe_class": "fair",
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
            "qoe_class": "good",
        },
        {
            "latitude": 47.0,
            "longitude": 47.0,
            "elevation_m": 8.0,
            "visible_satellites": 19,
            "serving_satellites": 17,
            "signal_loss_db": 2.0,
            "season": "summer",
            "weather": "clear",
            "qoe_class": "good",
        },
    ]


def test_all_churn_cycles_are_retained_and_dollar_weight_uses_the_mean() -> None:
    rows = churn_rows()
    profiles = aggregate_churn_profiles(rows)
    customer_rows = build_customer_rows(
        profiles,
        dict.fromkeys(profiles, "region-01-01"),
        dict.fromkeys(profiles, "Standard"),
        "churn@revision:train",
    )
    cycles = list(iter_billing_cycle_rows(rows, _CHURN_SOURCE, "churn@revision:train"))

    customer_a = next(row for row in customer_rows if row["customer_id"] == "customer-a")
    cycles_a = [row for row in cycles if row["customer_id"] == "customer-a"]

    assert len(cycles) == len(rows)
    assert len(cycles_a) == 2
    assert customer_a["dollar_weight"] == Decimal("150.000000")
    assert profiles["customer-a"].latest_cycle_start.isoformat() == "2024-02-01"
    assert profiles["customer-a"].latest_source_features["PaymentStatus"] == "Late"
    assert all("PaymentStatus" in _source_features(row) for row in cycles)


def test_non_finite_source_values_remain_stable_and_jsonb_compatible() -> None:
    raw_row = {"nan_value": float("nan"), "positive_infinity": float("inf")}

    normalized = normalize_raw_features(raw_row)
    key = stable_source_key("sample", _QOE_SOURCE, "qoe@revision:train", 0, raw_row)

    assert normalized == {"nan_value": "nan", "positive_infinity": "inf"}
    assert json.dumps(normalized, allow_nan=False)
    assert key == stable_source_key("sample", _QOE_SOURCE, "qoe@revision:train", 0, raw_row)


def test_qoe_region_and_source_grain_are_preserved_with_empirical_classes() -> None:
    churn = churn_rows()
    rows = qoe_rows()
    profiles = aggregate_churn_profiles(churn)
    qoe_profile = analyze_qoe_rows(lambda: iter(rows), seed=42)
    regions = assign_customer_regions(profiles, qoe_profile, coupling=1.0, seed=42)
    members = customers_by_region(regions)
    region_classes = choose_region_classes(qoe_profile, seed=42)
    samples = list(
        iter_qoe_sample_rows(
            rows,
            _QOE_SOURCE,
            "qoe@revision:train",
            qoe_profile,
            members,
            region_classes,
            seed=42,
        )
    )

    assert len(samples) == len(rows)
    assert len({row["qoe_sample_id"] for row in samples}) == len(rows)
    assert {(row["latitude"], row["longitude"]) for row in samples} == {
        (row["latitude"], row["longitude"]) for row in rows
    }
    assert all(row["region_id"] == regions[str(row["customer_id"])] for row in samples)
    assert all((row["season"], row["weather"]) in qoe_profile.class_pairs for row in samples)
    assert all("qoe_class" in _source_features(row) for row in samples)
    assert all(
        len({(row["season"], row["weather"]) for row in samples if row["region_id"] == region_id})
        == 1
        for region_id in qoe_profile.regions
    )


def test_regional_coupling_changes_the_churn_and_signal_loss_association() -> None:
    rows = churn_rows() * 20
    profiles = aggregate_churn_profiles(rows)
    qoe_profile = analyze_qoe_rows(lambda: iter(qoe_rows()), seed=13)

    independent = assign_customer_regions(profiles, qoe_profile, coupling=0.0, seed=13)
    coupled = assign_customer_regions(profiles, qoe_profile, coupling=1.0, seed=13)

    def difference(assignment: dict[str, str]) -> float:
        churned_scores = [
            qoe_profile.regions[assignment[customer_id]].mean_signal_loss_db
            for customer_id, profile in profiles.items()
            if profile.latest_churned
        ]
        retained_scores = [
            qoe_profile.regions[assignment[customer_id]].mean_signal_loss_db
            for customer_id, profile in profiles.items()
            if not profile.latest_churned
        ]
        return sum(churned_scores) / len(churned_scores) - sum(retained_scores) / len(
            retained_scores
        )

    assert difference(coupled) > difference(independent)


def test_qoe_missing_typed_value_uses_an_empirical_value_and_keeps_raw_source() -> None:
    rows = qoe_rows()
    rows[0] = {**rows[0], "visible_satellites": None}
    profiles = aggregate_churn_profiles(churn_rows())
    qoe_profile = analyze_qoe_rows(lambda: iter(rows), seed=7)
    regions = assign_customer_regions(profiles, qoe_profile, coupling=0.4, seed=7)
    samples = list(
        iter_qoe_sample_rows(
            rows,
            _QOE_SOURCE,
            "qoe@revision:train",
            qoe_profile,
            customers_by_region(regions),
            choose_region_classes(qoe_profile, seed=7),
            seed=7,
        )
    )

    imputed = next(
        row for row in samples if _source_features(row).get("visible_satellites") is None
    )
    assert imputed["visible_satellites"] in {10, 11, 19, 20}
    assert _source_features(imputed)["visible_satellites"] is None


def test_ra_fraud_transactions_keep_labels_and_a_single_plan_per_customer() -> None:
    churn = churn_rows()
    transactions = [
        {"Plan_Type": "Standard", "Fraud": 0, "call_minutes": 10},
        {"Plan_Type": "Premium", "Fraud": 1, "call_minutes": 120},
        {"Plan_Type": "Standard", "Fraud": 0, "call_minutes": 35},
        {"Plan_Type": "Premium", "Fraud": 0, "call_minutes": 22},
        {"Plan_Type": "Premium", "Fraud": 1, "call_minutes": 87},
        {"Plan_Type": "Standard", "Fraud": 1, "call_minutes": 62},
    ]
    profiles = aggregate_churn_profiles(churn)
    plan_counts = collect_plan_type_counts(transactions)
    plan_by_customer = assign_customer_plan_types(profiles, plan_counts, seed=42)
    customer_by_plan: dict[str, list[str]] = defaultdict(list)
    for customer_id, plan_type in plan_by_customer.items():
        customer_by_plan[plan_type].append(customer_id)
    output = list(
        iter_transaction_rows(
            transactions,
            _RAFM_SOURCE,
            "ra_fraud@revision:train",
            plan_counts,
            customer_by_plan,
            seed=42,
        )
    )

    assert len(output) == len(transactions)
    assert len({row["transaction_id"] for row in output}) == len(transactions)
    assert sum(bool(row["is_fraud"]) for row in output) == 3
    assert len({row["customer_id"] for row in output}) < len(output)
    for customer_id in {str(row["customer_id"]) for row in output}:
        customer_plan = plan_by_customer[customer_id]
        customer_transactions = [row for row in output if row["customer_id"] == customer_id]
        assert all(row["plan_type"] == customer_plan for row in customer_transactions)
    assert all("Fraud" in _source_features(row) for row in output)
