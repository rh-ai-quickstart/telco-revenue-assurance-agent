#!/usr/bin/env python3
"""
Data Fetch Tool — FastAPI service

Exposes two endpoints that hide PostgreSQL schema details from callers:
  POST /customers    – fetch customer records by ID list
  POST /transactions – fetch transaction records by customer ID list

Called by:
  - OGX agent (registered tool: fetch_customer_data)
  - Prediction tools (internal field fetching before model inference)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .db import SessionLocal, engine
from .models import CustomerFetchRequest, FetchResponse, TransactionFetchRequest
from .queries import fetch_customers, fetch_transactions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with SessionLocal() as session:
        await session.execute(text("SELECT 1"))
    log.info("Database connection verified")
    yield
    await engine.dispose()


app = FastAPI(
    title="Data Fetch Tool",
    description="Structured data access for customer and transaction records",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/customers/sample")
async def sample_customers(
    limit: int = 20,
    has_churned: bool | None = None,
    region_id: int | None = None,
    has_fraud: bool | None = None,
):
    """Return a sample of real customer IDs from the database."""
    try:
        async with SessionLocal() as session:
            filters = []
            params: dict = {"limit": min(limit, 20)}
            if has_churned is not None:
                filters.append("c.has_churned = :churned")
                params["churned"] = has_churned
            if region_id is not None:
                filters.append("c.region_id = :region_id")
                params["region_id"] = region_id
            if has_fraud is not None:
                # Join on the latest scored transaction per customer
                join = (
                    "JOIN ("
                    "  SELECT DISTINCT ON (customer_id) customer_id, predicted_fraud"
                    "  FROM transactions WHERE predicted_fraud IS NOT NULL"
                    "  ORDER BY customer_id, transaction_id DESC"
                    ") t ON t.customer_id = c.customer_id"
                )
                filters.append("t.predicted_fraud = :has_fraud")
                params["has_fraud"] = has_fraud
            else:
                join = ""
            where = f"WHERE {' AND '.join(filters)}" if filters else ""
            result = await session.execute(
                text(f"SELECT c.customer_id FROM customers c {join} {where} ORDER BY RANDOM() LIMIT :limit"),
                params,
            )
            ids = [row[0] for row in result]
        return {"customer_ids": ids, "count": len(ids)}
    except SQLAlchemyError:
        log.exception("Database error in /customers/sample")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/customers/count")
async def count_customers(
    region_id: int | None = None,
    has_fraud: bool | None = None,
    predicted_churn: bool | None = None,
):
    """Return a precise count of customers matching the given filters.

    Use this for cross-metric statistics (e.g. fraud-flagged customers who are
    also predicted to churn) without being limited by the sample cap.
    """
    try:
        async with SessionLocal() as session:
            filters = []
            params: dict = {}
            if region_id is not None:
                filters.append("c.region_id = :region_id")
                params["region_id"] = region_id
            if predicted_churn is not None:
                filters.append("c.predicted_churn = :predicted_churn")
                params["predicted_churn"] = predicted_churn
            if has_fraud is not None:
                join = (
                    "JOIN ("
                    "  SELECT DISTINCT ON (customer_id) customer_id, predicted_fraud"
                    "  FROM transactions WHERE predicted_fraud IS NOT NULL"
                    "  ORDER BY customer_id, transaction_id DESC"
                    ") t ON t.customer_id = c.customer_id"
                )
                filters.append("t.predicted_fraud = :has_fraud")
                params["has_fraud"] = has_fraud
            else:
                join = ""
            where = f"WHERE {' AND '.join(filters)}" if filters else ""
            result = await session.execute(
                text(f"SELECT COUNT(*) FROM customers c {join} {where}"),
                params,
            )
            count = result.scalar()
        return {"count": int(count or 0), "filters": {
            "region_id": region_id,
            "has_fraud": has_fraud,
            "predicted_churn": predicted_churn,
        }}
    except SQLAlchemyError:
        log.exception("Database error in /customers/count")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/customers/cross-count")
async def cross_count_customers(
    base_has_fraud: bool | None = None,
    base_predicted_churn: bool | None = None,
    cross_has_fraud: bool | None = None,
    cross_predicted_churn: bool | None = None,
    region_id: int | None = None,
):
    """Return base count + overlap count + overlap % in one call.

    Example: base_has_fraud=true, cross_predicted_churn=true
      → {"base_count": 11, "overlap_count": 0, "overlap_pct": 0.0,
         "label": "fraud-flagged customers who are also predicted to churn"}
    """
    async def _count(filters: dict) -> int:
        f, p = [], {}
        if region_id is not None:
            f.append("c.region_id = :region_id"); p["region_id"] = region_id
        if filters.get("predicted_churn") is not None:
            f.append("c.predicted_churn = :pc"); p["pc"] = filters["predicted_churn"]
        if filters.get("has_fraud") is not None:
            join = (
                "JOIN (SELECT DISTINCT ON (customer_id) customer_id, predicted_fraud "
                "FROM transactions WHERE predicted_fraud IS NOT NULL "
                "ORDER BY customer_id, transaction_id DESC) t ON t.customer_id = c.customer_id"
            )
            f.append("t.predicted_fraud = :hf"); p["hf"] = filters["has_fraud"]
        else:
            join = ""
        where = f"WHERE {' AND '.join(f)}" if f else ""
        async with SessionLocal() as session:
            r = await session.execute(
                text(f"SELECT COUNT(*) FROM customers c {join} {where}"), p
            )
            return int(r.scalar() or 0)

    try:
        base_filters = {k: v for k, v in {"has_fraud": base_has_fraud, "predicted_churn": base_predicted_churn}.items() if v is not None}
        overlap_filters = {**base_filters}
        if cross_has_fraud is not None:
            overlap_filters["has_fraud"] = cross_has_fraud
        if cross_predicted_churn is not None:
            overlap_filters["predicted_churn"] = cross_predicted_churn

        base = await _count(base_filters)
        overlap = await _count(overlap_filters)
        pct = round(overlap / base * 100, 1) if base > 0 else 0.0

        parts = []
        if base_has_fraud is not None:
            parts.append("fraud-flagged" if base_has_fraud else "clean")
        if base_predicted_churn is not None:
            parts.append("predicted-churn" if base_predicted_churn else "predicted-stable")
        base_label = " + ".join(parts) + " customers"
        cross_parts = []
        if cross_has_fraud is not None:
            cross_parts.append("fraud-flagged" if cross_has_fraud else "clean")
        if cross_predicted_churn is not None:
            cross_parts.append("predicted to churn" if cross_predicted_churn else "predicted to stay")
        cross_label = " and ".join(cross_parts)

        if base == 0:
            interpretation = "No customers matched the base filter — cannot determine overlap."
        elif pct >= 50:
            interpretation = (
                f"{overlap} of {base} {base_label} are also {cross_label} ({pct}%). "
                "High overlap — these may be billing disputes rather than actual fraud. "
                "Review before blocking accounts."
            )
        else:
            interpretation = (
                f"{overlap} of {base} {base_label} are also {cross_label} ({pct}%). "
                "Low overlap — fraud and churn affect different customer segments. "
                "Treat as independent problems; anti-fraud measures will not reduce churn."
            )

        return {
            "base_count": base,
            "overlap_count": overlap,
            "overlap_pct": pct,
            "interpretation": interpretation,
        }
    except SQLAlchemyError:
        log.exception("Database error in /customers/cross-count")
        raise HTTPException(status_code=500, detail="Database error")


@app.post("/customers", response_model=FetchResponse)
async def get_customers(req: CustomerFetchRequest):
    try:
        async with SessionLocal() as session:
            records = await fetch_customers(session, req.customer_ids, req.fields)
        return FetchResponse(records=records, count=len(records))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except SQLAlchemyError:
        log.exception("Database error in /customers")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/customers/grouped")
async def customers_grouped():
    """Return per-region aggregated stats for agent investigation."""
    try:
        async with SessionLocal() as session:
            result = await session.execute(text("""
                SELECT
                    c.region_id,
                    COUNT(*)                                                                        AS customer_count,
                    SUM(CASE WHEN c.predicted_churn THEN 1 ELSE 0 END)                             AS churn_count,
                    ROUND(AVG(CASE WHEN c.predicted_churn THEN 1.0 ELSE 0.0 END)::numeric, 4)      AS churn_rate,
                    ROUND(AVG(c.predicted_qoe_score)::numeric, 1)                                  AS avg_qoe_score,
                    ROUND(AVG(c.avg_bill_amount)::numeric, 2)                                      AS avg_bill_amount,
                    ROUND(SUM(CASE WHEN COALESCE(c.predicted_churn, false)
                              THEN c.avg_bill_amount ELSE 0 END)::numeric, 2)                      AS revenue_at_risk,
                    MODE() WITHIN GROUP (ORDER BY c.season)                                         AS season,
                    COALESCE(COUNT(t.customer_id), 0)                                               AS transaction_count,
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
                ORDER BY churn_rate DESC
            """))
            rows = result.mappings().all()
        groups = [dict(row) for row in rows]

        def _summary(r: dict, keys: list) -> dict:
            return {k: r[k] for k in keys}

        _churn_keys = ["region_id", "churn_rate", "churn_count", "revenue_at_risk", "avg_qoe_score"]
        _fraud_keys  = ["region_id", "fraud_rate", "fraud_count", "transaction_count"]
        _qoe_keys    = ["region_id", "avg_qoe_score", "churn_rate", "fraud_rate"]

        worst_churn = max(groups, key=lambda r: r["churn_rate"] or 0, default=None)
        best_churn  = min(groups, key=lambda r: r["churn_rate"] or 1, default=None)
        worst_fraud = max(groups, key=lambda r: r["fraud_rate"] or 0, default=None)
        best_fraud  = min(groups, key=lambda r: r["fraud_rate"] or 1, default=None)
        worst_qoe   = min(groups, key=lambda r: r["avg_qoe_score"] or 100, default=None)
        best_qoe    = max(groups, key=lambda r: r["avg_qoe_score"] or 0, default=None)

        return {
            "group_by": "region_id",
            "groups": groups,
            "count": len(groups),
            "worst_churn_region": _summary(worst_churn, _churn_keys) if worst_churn else None,
            "best_churn_region":  _summary(best_churn,  _churn_keys) if best_churn  else None,
            "worst_fraud_region": _summary(worst_fraud, _fraud_keys) if worst_fraud else None,
            "best_fraud_region":  _summary(best_fraud,  _fraud_keys) if best_fraud  else None,
            "worst_qoe_region":   _summary(worst_qoe,   _qoe_keys)   if worst_qoe   else None,
            "best_qoe_region":    _summary(best_qoe,    _qoe_keys)   if best_qoe    else None,
        }
    except SQLAlchemyError:
        log.exception("Database error in /customers/grouped")
        raise HTTPException(status_code=500, detail="Database error")


@app.post("/transactions", response_model=FetchResponse)
async def get_transactions(req: TransactionFetchRequest):
    try:
        async with SessionLocal() as session:
            records = await fetch_transactions(
                session,
                req.customer_ids,
                req.fields,
                req.limit_per_customer,
            )
        return FetchResponse(records=records, count=len(records))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except SQLAlchemyError:
        log.exception("Database error in /transactions")
        raise HTTPException(status_code=500, detail="Database error")
