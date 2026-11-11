from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Ordered column definitions — order matches DB DDL for readability in responses.
# The frozensets are used for O(1) membership checks during field validation.
_CUSTOMER_COLUMNS: tuple[str, ...] = (
    "customer_id", "has_churned", "avg_bill_amount",
    "call_minutes", "data_usage_gb", "sms_count", "internet_sessions",
    "total_bill_amount", "payment_status", "late_fee", "discounts_applied",
    "support_interaction_count", "primary_issue_type", "resolution_time_minutes",
    "satisfaction_score", "escalation_flag", "support_channel",
    "region_id", "latitude", "longitude", "elevation_m",
    "visible_satellites", "serving_satellites", "signal_loss_db",
    "season", "weather", "qoe_score", "qoe_download_mbps",
    "qoe_upload_mbps", "qoe_latency_ms",
)
CUSTOMER_FIELDS: frozenset[str] = frozenset(_CUSTOMER_COLUMNS)

_TRANSACTION_COLUMNS: tuple[str, ...] = (
    "transaction_id", "customer_id",
    "call_duration", "data_usage", "sms_count",
    "roaming_indicator", "mobile_wallet_use", "plan_type",
    "cost", "cellular_location_distance", "personal_pin_used",
    "avg_call_duration", "avg_data_usage", "avg_cost", "fraud",
)
TRANSACTION_FIELDS: frozenset[str] = frozenset(_TRANSACTION_COLUMNS)


class CustomerFetchRequest(BaseModel):
    customer_ids: list[str] = Field(..., min_length=1, max_length=1000)
    fields: list[str] | None = Field(
        default=None,
        description="Columns to return. Omit for all columns.",
    )


class TransactionFetchRequest(BaseModel):
    customer_ids: list[str] = Field(..., min_length=1, max_length=1000)
    fields: list[str] | None = Field(
        default=None,
        description="Columns to return. Omit for all columns.",
    )
    limit_per_customer: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Max transactions returned per customer. Omit for all.",
    )


class FetchResponse(BaseModel):
    records: list[dict[str, Any]]
    count: int
