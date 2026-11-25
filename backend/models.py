from __future__ import annotations

from pydantic import BaseModel, Field


# ── Chat ─────────────────────────────────────────────────────────────────────

class SessionResponse(BaseModel):
    session_id: str


class TurnRequest(BaseModel):
    message: str = Field(..., min_length=1)


# ── Dashboard metrics ─────────────────────────────────────────────────────────

class CustomerMetrics(BaseModel):
    total: int
    churn_rate: float
    avg_bill_amount: float
    active_count: int       # has_churned=False — candidates for prediction
    revenue_at_risk: float  # SUM(avg_bill_amount) for active customers


class TransactionMetrics(BaseModel):
    total: int
    fraud_rate: float
    fraud_count: int
    distinct_customers: int


class RegionMetrics(BaseModel):
    region_id: int
    customer_count: int
    churn_count: int
    churn_rate: float
    avg_qoe_score: float    # 0-100 scale
    season: str
    fraud_count: int
    fraud_rate: float


class QoeDistribution(BaseModel):
    poor: int   # bottom third of QoE scores (below 33rd percentile)
    fair: int   # middle third (33rd–66th percentile)
    good: int   # top third (above 66th percentile)


class DashboardMetrics(BaseModel):
    customers: CustomerMetrics
    transactions: TransactionMetrics
    regions: list[RegionMetrics]
    avg_qoe_score: float
    qoe_distribution: QoeDistribution
