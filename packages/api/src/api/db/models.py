"""SQLAlchemy models for the approved application schema."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ScoringRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelFamily(StrEnum):
    CHURN = "churn"
    FRAUD = "fraud"
    QOE = "qoe"


class Base(DeclarativeBase):
    metadata = MetaData(schema="app")


scoring_run_status_type = SAEnum(
    ScoringRunStatus,
    name="scoring_run_status",
    schema="app",
    values_callable=lambda enum_class: [item.value for item in enum_class],
    native_enum=True,
)
model_family_type = SAEnum(
    ModelFamily,
    name="model_family",
    schema="app",
    values_callable=lambda enum_class: [item.value for item in enum_class],
    native_enum=True,
)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("length(btrim(customer_id)) > 0"),
        CheckConstraint("plan_type IS NULL OR length(btrim(plan_type)) > 0"),
        CheckConstraint("dollar_weight >= 0"),
        CheckConstraint("length(btrim(region_id)) > 0"),
        CheckConstraint("length(btrim(source_dataset_revision)) > 0"),
        CheckConstraint("jsonb_typeof(source_features) = 'object'"),
        Index("ix_customers_region", "region_id", "customer_id"),
        {
            "comment": (
                "One row per CustomerID. dollar_weight is the mean TotalBillAmount across every "
                "retained billing cycle."
            )
        },
    )

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    plan_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    dollar_weight: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    region_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_dataset_revision: Mapped[str] = mapped_column(Text, nullable=False)
    source_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingCycle(Base):
    __tablename__ = "billing_cycles"
    __table_args__ = (
        CheckConstraint("length(btrim(billing_cycle_id)) > 0"),
        CheckConstraint("total_bill_amount >= 0"),
        CheckConstraint("length(btrim(source_dataset_revision)) > 0"),
        CheckConstraint("jsonb_typeof(source_features) = 'object'"),
        UniqueConstraint("customer_id", "billing_cycle_start", "billing_cycle_id"),
        Index(
            "ix_billing_cycles_customer_latest",
            "customer_id",
            text("billing_cycle_start DESC"),
            "billing_cycle_id",
        ),
        {
            "comment": (
                "One source churn billing-cycle row; original fields and labels remain in "
                "source_features."
            )
        },
    )

    billing_cycle_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    billing_cycle_start: Mapped[date] = mapped_column(Date, nullable=False)
    total_bill_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    source_dataset_revision: Mapped[str] = mapped_column(Text, nullable=False)
    source_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("length(btrim(transaction_id)) > 0"),
        CheckConstraint("length(btrim(plan_type)) > 0"),
        CheckConstraint("length(btrim(source_dataset_revision)) > 0"),
        CheckConstraint("jsonb_typeof(source_features) = 'object'"),
        Index("ix_transactions_customer", "customer_id", "transaction_id"),
        Index("ix_transactions_fraud", "is_fraud", "transaction_id"),
        {
            "comment": (
                "One source RAFM transaction. Stable loader key and source fields are retained."
            )
        },
    )

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    plan_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_fraud: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_dataset_revision: Mapped[str] = mapped_column(Text, nullable=False)
    source_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class QoeSample(Base):
    __tablename__ = "qoe_samples"
    __table_args__ = (
        CheckConstraint("length(btrim(qoe_sample_id)) > 0"),
        CheckConstraint("length(btrim(region_id)) > 0"),
        CheckConstraint("latitude BETWEEN -90 AND 90"),
        CheckConstraint("longitude BETWEEN -180 AND 180"),
        CheckConstraint("visible_satellites >= 0"),
        CheckConstraint("serving_satellites >= 0"),
        CheckConstraint("length(btrim(season)) > 0"),
        CheckConstraint("length(btrim(weather)) > 0"),
        CheckConstraint("length(btrim(source_dataset_revision)) > 0"),
        CheckConstraint("jsonb_typeof(source_features) = 'object'"),
        Index("ix_qoe_samples_customer", "customer_id", "qoe_sample_id"),
        {"comment": "One Starlink source sample assigned to a synthetic customer and region."},
    )

    qoe_sample_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        Text, ForeignKey("app.customers.customer_id", ondelete="RESTRICT"), nullable=False
    )
    region_id: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    elevation_m: Mapped[float] = mapped_column(Float, nullable=False)
    visible_satellites: Mapped[int] = mapped_column(Integer, nullable=False)
    serving_satellites: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_loss_db: Mapped[float] = mapped_column(Float, nullable=False)
    season: Mapped[str] = mapped_column(Text, nullable=False)
    weather: Mapped[str] = mapped_column(Text, nullable=False)
    source_dataset_revision: Mapped[str] = mapped_column(Text, nullable=False)
    source_features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScoringRun(Base):
    __tablename__ = "scoring_runs"
    __table_args__ = (
        CheckConstraint("length(btrim(idempotency_key)) > 0"),
        CheckConstraint("request_fingerprint ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("churn_processed_rows >= 0"),
        CheckConstraint("churn_total_rows >= churn_processed_rows"),
        CheckConstraint("fraud_processed_rows >= 0"),
        CheckConstraint("fraud_total_rows >= fraud_processed_rows"),
        CheckConstraint("qoe_processed_rows >= 0"),
        CheckConstraint("qoe_total_rows >= qoe_processed_rows"),
        CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) "
            "OR (status IN ('completed', 'failed') AND completed_at IS NOT NULL)"
        ),
        Index("ix_scoring_runs_created", text("created_at DESC"), "run_id"),
        Index("ix_scoring_runs_status", "status", "created_at"),
        {"comment": "Explicit analyst-triggered run with monotonic per-model progress counters."},
    )

    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[ScoringRunStatus] = mapped_column(
        scoring_run_status_type,
        nullable=False,
        server_default=text("'queued'::app.scoring_run_status"),
    )
    churn_processed_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    churn_total_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    fraud_processed_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    fraud_total_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    qoe_processed_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    qoe_total_rows: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ChatRequest(Base):
    __tablename__ = "chat_requests"
    __table_args__ = (
        CheckConstraint("message_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("status IN ('in_progress', 'completed', 'rejected')"),
        CheckConstraint("response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'"),
        CheckConstraint(
            "(status IN ('in_progress', 'rejected') AND response_payload IS NULL "
            "AND completed_at IS NULL) "
            "OR (status = 'completed' AND response_payload IS NOT NULL "
            "AND completed_at IS NOT NULL)"
        ),
        {
            "comment": (
                "Durable backend idempotency records scoped to an OGX conversation; binds a "
                "request ID to a message hash across explicit rejections and stores completed "
                "public responses."
            )
        },
    )

    session_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    request_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    message_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_progress'"))
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100"),
        CheckConstraint("length(btrim(model_version)) > 0"),
        CheckConstraint("feature_snapshot_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("jsonb_typeof(model_output) = 'object'"),
        CheckConstraint(
            "(model_type = 'churn' AND customer_id IS NOT NULL AND transaction_id IS NULL "
            "AND class_id IS NOT NULL AND is_fraud IS NULL AND score IS NULL) OR "
            "(model_type = 'fraud' AND customer_id IS NULL AND transaction_id IS NOT NULL "
            "AND class_id IS NOT NULL AND class_id IN (0, 1) AND is_fraud IS NOT NULL "
            "AND is_fraud = (class_id = 1) AND score IS NULL) OR "
            "(model_type = 'qoe' AND customer_id IS NOT NULL AND transaction_id IS NULL "
            "AND class_id IS NULL AND is_fraud IS NULL AND score IS NOT NULL)"
        ),
        Index(
            "uq_predictions_run_model_customer",
            "scoring_run_id",
            "model_type",
            "customer_id",
            unique=True,
            postgresql_where=text("customer_id IS NOT NULL"),
        ),
        Index(
            "uq_predictions_run_model_transaction",
            "scoring_run_id",
            "model_type",
            "transaction_id",
            unique=True,
            postgresql_where=text("transaction_id IS NOT NULL"),
        ),
        Index(
            "ix_predictions_run_model_subject",
            "scoring_run_id",
            "model_type",
            "customer_id",
            "transaction_id",
        ),
        Index("ix_predictions_created", text("created_at DESC"), "prediction_id"),
    )

    prediction_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    scoring_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("app.scoring_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    model_type: Mapped[ModelFamily] = mapped_column(model_family_type, nullable=False)
    customer_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("app.customers.customer_id", ondelete="RESTRICT"), nullable=True
    )
    transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("app.transactions.transaction_id", ondelete="RESTRICT"), nullable=True
    )
    class_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_fraud: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    feature_snapshot_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    model_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
