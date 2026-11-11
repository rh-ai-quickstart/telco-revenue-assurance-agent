"""
Unit tests for backend/routes/metrics.py.

The database session is fully mocked — no real PostgreSQL connection needed.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")


def _make_row(data: dict) -> MagicMock:
    row = MagicMock()
    row.__getitem__ = lambda self, k: data[k]
    return row


def _mock_session(cust_row, txn_row, region_rows):
    """Build an async context-manager mock that returns three sequential query results."""
    def _make_result(row_or_rows, *, many=False):
        result = MagicMock()
        mapping = MagicMock()
        if many:
            mapping.all.return_value = row_or_rows
        else:
            mapping.one.return_value = row_or_rows
        result.mappings.return_value = mapping
        return result

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _make_result(cust_row),
        _make_result(txn_row),
        _make_result(region_rows, many=True),
    ])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_dashboard_returns_correct_structure():
    from backend.routes.metrics import dashboard

    cust  = _make_row({"total": 1000, "churn_rate": 0.25, "avg_bill_amount": 75.50, "active_count": 750})
    txn   = _make_row({"total": 50000, "fraud_rate": 0.02, "distinct_customers": 900})
    regs  = [_make_row({"region_id": 0, "customer_count": 250, "churn_rate": 0.35,
                         "avg_qoe_score": 45.0, "season": "Winter", "weather": "Cloudy"})]

    with patch("backend.routes.metrics.SessionLocal", return_value=_mock_session(cust, txn, regs)):
        result = await dashboard()

    assert result.customers.total == 1000
    assert result.customers.churn_rate == 0.25
    assert result.customers.active_count == 750
    assert result.transactions.fraud_rate == 0.02
    assert len(result.regions) == 1
    assert result.regions[0].region_id == 0
    assert result.regions[0].season == "Winter"


@pytest.mark.asyncio
async def test_dashboard_rounds_floats():
    from backend.routes.metrics import dashboard

    cust = _make_row({"total": 500, "churn_rate": 0.254999, "avg_bill_amount": 80.123456, "active_count": 375})
    txn  = _make_row({"total": 10000, "fraud_rate": 0.019876, "distinct_customers": 490})
    regs = [_make_row({"region_id": 1, "customer_count": 500, "churn_rate": 0.254999,
                        "avg_qoe_score": 60.123456, "season": "Summer", "weather": "Clear"})]

    with patch("backend.routes.metrics.SessionLocal", return_value=_mock_session(cust, txn, regs)):
        result = await dashboard()

    assert result.customers.churn_rate == round(0.254999, 4)
    assert result.transactions.fraud_rate == round(0.019876, 4)


@pytest.mark.asyncio
async def test_dashboard_handles_null_aggregates():
    from backend.routes.metrics import dashboard

    # Simulate empty table (all aggregates return None)
    cust = _make_row({"total": 0, "churn_rate": None, "avg_bill_amount": None, "active_count": 0})
    txn  = _make_row({"total": 0, "fraud_rate": None, "distinct_customers": 0})
    regs = []

    with patch("backend.routes.metrics.SessionLocal", return_value=_mock_session(cust, txn, regs)):
        result = await dashboard()

    assert result.customers.churn_rate == 0.0
    assert result.transactions.fraud_rate == 0.0
    assert result.regions == []
