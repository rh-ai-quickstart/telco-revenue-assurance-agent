"""Static entity and field mappings; request values never become SQL identifiers."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.sql.elements import ColumnElement

from data_fetch_tool.schemas import Entity, FieldAlias

ValueKind = Literal["string", "integer", "number", "boolean", "date", "uuid", "feature_map"]


@dataclass(frozen=True)
class FieldSpec:
    """A public alias and its fixed SQL expression and filter/sort behavior."""

    expression: ColumnElement[Any]
    kind: ValueKind
    nullable: bool = False
    ordered: bool = True


metadata = MetaData(schema="app")

customers = Table(
    "customers",
    metadata,
    Column("customer_id", Text, primary_key=True),
    Column("plan_type", Text, nullable=True),
    Column("dollar_weight", Numeric(18, 6), nullable=False),
    Column("region_id", Text, nullable=False),
    Column("source_dataset_revision", Text, nullable=False),
    Column("source_features", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

billing_cycles = Table(
    "billing_cycles",
    metadata,
    Column("billing_cycle_id", Text, primary_key=True),
    Column("customer_id", Text, nullable=False),
    Column("billing_cycle_start", Date, nullable=False),
    Column("total_bill_amount", Numeric(18, 6), nullable=False),
    Column("source_dataset_revision", Text, nullable=False),
    Column("source_features", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

transactions = Table(
    "transactions",
    metadata,
    Column("transaction_id", Text, primary_key=True),
    Column("customer_id", Text, nullable=False),
    Column("plan_type", Text, nullable=False),
    Column("is_fraud", Boolean, nullable=False),
    Column("source_dataset_revision", Text, nullable=False),
    Column("source_features", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

qoe_samples = Table(
    "qoe_samples",
    metadata,
    Column("qoe_sample_id", Text, primary_key=True),
    Column("customer_id", Text, nullable=False),
    Column("region_id", Text, nullable=False),
    Column("latitude", Double, nullable=False),
    Column("longitude", Double, nullable=False),
    Column("elevation_m", Double, nullable=False),
    Column("visible_satellites", Integer, nullable=False),
    Column("serving_satellites", Integer, nullable=False),
    Column("signal_loss_db", Double, nullable=False),
    Column("season", Text, nullable=False),
    Column("weather", Text, nullable=False),
    Column("source_dataset_revision", Text, nullable=False),
    Column("source_features", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

predictions = Table(
    "predictions",
    metadata,
    Column("prediction_id", Uuid(as_uuid=False), primary_key=True),
    Column("scoring_run_id", Uuid(as_uuid=False), nullable=False),
    Column(
        "model_type",
        ENUM("churn", "fraud", "qoe", name="model_family", schema="app"),
        nullable=False,
    ),
    Column("customer_id", Text, nullable=True),
    Column("transaction_id", Text, nullable=True),
    Column("class_id", BigInteger, nullable=True),
    Column("is_fraud", Boolean, nullable=True),
    Column("score", Numeric(8, 4), nullable=True),
    Column("model_version", Text, nullable=False),
    Column("feature_snapshot_sha256", CHAR(64), nullable=False),
    Column("model_output", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

ENTITY_TABLES: Mapping[Entity, Table] = {
    "customers": customers,
    "billing_cycles": billing_cycles,
    "transactions": transactions,
    "qoe_samples": qoe_samples,
    "predictions": predictions,
}

ENTITY_ID_COLUMNS: Mapping[Entity, ColumnElement[Any]] = {
    "customers": customers.c.customer_id,
    "billing_cycles": billing_cycles.c.billing_cycle_id,
    "transactions": transactions.c.transaction_id,
    "qoe_samples": qoe_samples.c.qoe_sample_id,
    "predictions": predictions.c.prediction_id,
}


def _field(
    expression: ColumnElement[Any],
    kind: ValueKind,
    *,
    nullable: bool = False,
    ordered: bool = True,
) -> FieldSpec:
    return FieldSpec(expression, kind, nullable=nullable, ordered=ordered)


_latest_churn_features = (
    select(billing_cycles.c.source_features)
    .where(billing_cycles.c.customer_id == customers.c.customer_id)
    .order_by(billing_cycles.c.billing_cycle_start.desc(), billing_cycles.c.billing_cycle_id.asc())
    .limit(1)
    .correlate(customers)
    .scalar_subquery()
)

_customer_qoe_features = (
    select(
        func.jsonb_build_object(
            "latitude",
            qoe_samples.c.latitude,
            "longitude",
            qoe_samples.c.longitude,
            "elevation_m",
            qoe_samples.c.elevation_m,
            "visible_satellites",
            qoe_samples.c.visible_satellites,
            "serving_satellites",
            qoe_samples.c.serving_satellites,
            "signal_loss_db",
            qoe_samples.c.signal_loss_db,
            "season",
            qoe_samples.c.season,
            "weather",
            qoe_samples.c.weather,
        )
    )
    .where(qoe_samples.c.customer_id == customers.c.customer_id)
    .order_by(qoe_samples.c.qoe_sample_id.asc())
    .limit(1)
    .correlate(customers)
    .scalar_subquery()
)

_sample_qoe_features = func.jsonb_build_object(
    "latitude",
    qoe_samples.c.latitude,
    "longitude",
    qoe_samples.c.longitude,
    "elevation_m",
    qoe_samples.c.elevation_m,
    "visible_satellites",
    qoe_samples.c.visible_satellites,
    "serving_satellites",
    qoe_samples.c.serving_satellites,
    "signal_loss_db",
    qoe_samples.c.signal_loss_db,
    "season",
    qoe_samples.c.season,
    "weather",
    qoe_samples.c.weather,
)

FIELD_ALLOWLIST: Mapping[Entity, Mapping[FieldAlias, FieldSpec]] = {
    "customers": {
        "customer_id": _field(customers.c.customer_id, "string"),
        "plan_type": _field(customers.c.plan_type, "string", nullable=True),
        "dollar_weight": _field(customers.c.dollar_weight, "number"),
        "region_id": _field(customers.c.region_id, "string"),
        "churn_features": _field(_latest_churn_features, "feature_map", ordered=False),
        "qoe_features": _field(_customer_qoe_features, "feature_map", nullable=True, ordered=False),
    },
    "billing_cycles": {
        "billing_cycle_id": _field(billing_cycles.c.billing_cycle_id, "string"),
        "customer_id": _field(billing_cycles.c.customer_id, "string"),
        "billing_cycle_start": _field(billing_cycles.c.billing_cycle_start, "date"),
        "total_bill_amount": _field(billing_cycles.c.total_bill_amount, "number"),
        "churn_features": _field(billing_cycles.c.source_features, "feature_map", ordered=False),
    },
    "transactions": {
        "transaction_id": _field(transactions.c.transaction_id, "string"),
        "customer_id": _field(transactions.c.customer_id, "string"),
        "plan_type": _field(transactions.c.plan_type, "string"),
        "fraud_features": _field(transactions.c.source_features, "feature_map", ordered=False),
    },
    "qoe_samples": {
        "qoe_sample_id": _field(qoe_samples.c.qoe_sample_id, "string"),
        "customer_id": _field(qoe_samples.c.customer_id, "string"),
        "region_id": _field(qoe_samples.c.region_id, "string"),
        "qoe_features": _field(_sample_qoe_features, "feature_map", ordered=False),
    },
    "predictions": {
        "prediction_id": _field(predictions.c.prediction_id, "uuid"),
        "scoring_run_id": _field(predictions.c.scoring_run_id, "uuid"),
        "model_type": _field(predictions.c.model_type, "string"),
        "customer_id": _field(predictions.c.customer_id, "string", nullable=True),
        "transaction_id": _field(predictions.c.transaction_id, "string", nullable=True),
        "class_id": _field(predictions.c.class_id, "integer", nullable=True),
        "is_fraud": _field(
            predictions.c.is_fraud,
            "boolean",
            nullable=True,
            ordered=False,
        ),
        "score": _field(predictions.c.score, "number", nullable=True),
        "model_version": _field(predictions.c.model_version, "string"),
    },
}

FILTER_ONLY_FIELDS: Mapping[Entity, Mapping[FieldAlias, FieldSpec]] = {
    "transactions": {
        "is_fraud": _field(transactions.c.is_fraud, "boolean", ordered=False),
    },
}


DEFAULT_FIELDS: Mapping[Entity, tuple[FieldAlias, ...]] = {
    "customers": ("customer_id", "plan_type", "dollar_weight", "region_id"),
    "billing_cycles": (
        "billing_cycle_id",
        "customer_id",
        "billing_cycle_start",
        "total_bill_amount",
    ),
    "transactions": ("transaction_id", "customer_id", "plan_type"),
    "qoe_samples": ("qoe_sample_id", "customer_id", "region_id"),
    "predictions": (
        "prediction_id",
        "scoring_run_id",
        "model_type",
        "customer_id",
        "transaction_id",
        "class_id",
        "is_fraud",
        "score",
        "model_version",
    ),
}


def feature_value_is_allowed(entity: Entity, alias: FieldAlias, key: str) -> bool:
    """Remove target and source-date keys from derived feature maps."""
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    if alias == "churn_features":
        return (
            normalized
            not in {
                "customerid",
                "haschurned",
                "billingcycleid",
                "billingcyclestart",
                "cycleid",
                "cyclestart",
                "date",
                "recorddate",
            }
            and "date" not in normalized
            and "cycle" not in normalized
        )
    if alias == "fraud_features":
        return normalized not in {
            "fraud",
            "isfraud",
            "fraudlabel",
            "isfraudulent",
            "label",
            "class",
            "classid",
            "target",
        } and not normalized.endswith("label")
    if alias == "qoe_features":
        return key in {
            "latitude",
            "longitude",
            "elevation_m",
            "visible_satellites",
            "serving_satellites",
            "signal_loss_db",
            "season",
            "weather",
        }
    return False
