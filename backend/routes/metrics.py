from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from ..db import SessionLocal
from ..models import (
    CustomerMetrics,
    DashboardMetrics,
    QoeDistribution,
    RegionMetrics,
    TransactionMetrics,
)

router = APIRouter(prefix="/api/metrics")

_CUSTOMER_SQL = text("""
    SELECT
        COUNT(*)                                                                        AS total,
        AVG(CASE WHEN predicted_churn THEN 1.0 ELSE 0.0 END)                           AS churn_rate,
        AVG(avg_bill_amount)                                                            AS avg_bill_amount,
        SUM(CASE WHEN NOT COALESCE(predicted_churn, false) THEN 1 ELSE 0 END)          AS active_count,
        SUM(CASE WHEN COALESCE(predicted_churn, false) THEN avg_bill_amount ELSE 0.0 END) AS revenue_at_risk
    FROM customers
    WHERE predicted_churn IS NOT NULL
""")

_TRANSACTION_SQL = text("""
    SELECT
        (SELECT COUNT(*) FROM transactions)                             AS total,
        AVG(CASE WHEN predicted_fraud THEN 1.0 ELSE 0.0 END)           AS fraud_rate,
        SUM(CASE WHEN predicted_fraud THEN 1 ELSE 0 END)               AS fraud_count,
        COUNT(DISTINCT customer_id)                                     AS distinct_customers
    FROM transactions
    WHERE predicted_fraud IS NOT NULL
""")

# DISTINCT ON picks the latest scored transaction per customer for fraud aggregation.
# MODE() WITHIN GROUP is PostgreSQL-specific; returns the most frequent season per region.
_REGION_SQL = text("""
    SELECT
        c.region_id,
        COUNT(*)                                                                        AS customer_count,
        SUM(CASE WHEN c.predicted_churn THEN 1 ELSE 0 END)                             AS churn_count,
        ROUND(AVG(CASE WHEN c.predicted_churn THEN 1.0 ELSE 0.0 END)::numeric, 4)      AS churn_rate,
        ROUND(AVG(c.predicted_qoe_score)::numeric, 1)                                  AS avg_qoe_score,
        MODE() WITHIN GROUP (ORDER BY c.season)                                         AS season,
        COALESCE(SUM(CASE WHEN t.predicted_fraud THEN 1 ELSE 0 END), 0)                AS fraud_count,
        ROUND(
            COALESCE(
                SUM(CASE WHEN t.predicted_fraud THEN 1.0 ELSE 0.0 END) / NULLIF(COUNT(*), 0),
                0
            )::numeric, 4
        )                                                                               AS fraud_rate
    FROM customers c
    LEFT JOIN (
        SELECT DISTINCT ON (customer_id) customer_id, predicted_fraud
        FROM transactions
        WHERE predicted_fraud IS NOT NULL
        ORDER BY customer_id, transaction_id DESC
    ) t ON t.customer_id = c.customer_id
    WHERE c.predicted_churn IS NOT NULL AND c.predicted_qoe_score IS NOT NULL
    GROUP BY c.region_id
    ORDER BY c.region_id
""")

# QoE distribution using the same fixed thresholds as the predictor service
# (QOE_POOR_THRESHOLD=65, QOE_GOOD_THRESHOLD=80) for consistency.
_QOE_SQL = text("""
    SELECT
        AVG(predicted_qoe_score)                                                            AS avg_qoe_score,
        SUM(CASE WHEN predicted_qoe_score < 65 THEN 1 ELSE 0 END)                          AS poor,
        SUM(CASE WHEN predicted_qoe_score >= 65 AND predicted_qoe_score < 80 THEN 1 ELSE 0 END) AS fair,
        SUM(CASE WHEN predicted_qoe_score >= 80 THEN 1 ELSE 0 END)                         AS good
    FROM customers
    WHERE predicted_qoe_score IS NOT NULL
""")


@router.get("/dashboard", response_model=DashboardMetrics)
async def dashboard():
    async with SessionLocal() as session:
        c = (await session.execute(_CUSTOMER_SQL)).mappings().one()
        t = (await session.execute(_TRANSACTION_SQL)).mappings().one()
        r = (await session.execute(_REGION_SQL)).mappings().all()
        q = (await session.execute(_QOE_SQL)).mappings().one()

    return DashboardMetrics(
        customers=CustomerMetrics(
            total=c["total"],
            churn_rate=round(c["churn_rate"] or 0.0, 4),
            avg_bill_amount=round(c["avg_bill_amount"] or 0.0, 2),
            active_count=c["active_count"],
            revenue_at_risk=round(c["revenue_at_risk"] or 0.0, 2),
        ),
        transactions=TransactionMetrics(
            total=t["total"],
            fraud_rate=round(t["fraud_rate"] or 0.0, 4),
            fraud_count=int(t["fraud_count"] or 0),
            distinct_customers=t["distinct_customers"],
        ),
        regions=[
            RegionMetrics(
                region_id=row["region_id"],
                customer_count=row["customer_count"],
                churn_count=int(row["churn_count"] or 0),
                churn_rate=round(row["churn_rate"] or 0.0, 4),
                avg_qoe_score=round(row["avg_qoe_score"] or 0.0, 1),
                season=row["season"] or "",
                fraud_count=int(row["fraud_count"] or 0),
                fraud_rate=round(row["fraud_rate"] or 0.0, 4),
            )
            for row in r
        ],
        avg_qoe_score=round(q["avg_qoe_score"] or 0.0, 1),
        qoe_distribution=QoeDistribution(
            poor=int(q["poor"] or 0),
            fair=int(q["fair"] or 0),
            good=int(q["good"] or 0),
        ),
    )
