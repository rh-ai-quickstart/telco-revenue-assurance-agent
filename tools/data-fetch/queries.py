from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    CUSTOMER_FIELDS,
    TRANSACTION_FIELDS,
    _CUSTOMER_COLUMNS,
    _TRANSACTION_COLUMNS,
)


def _resolve_fields(
    requested: list[str] | None,
    allowed: frozenset[str],
    default_order: tuple[str, ...],
) -> list[str]:
    """Return validated column list; raises ValueError on unknown names."""
    if not requested:
        return list(default_order)
    unknown = sorted(set(requested) - allowed)
    if unknown:
        raise ValueError(f"Unknown fields: {unknown}")
    return requested


def _quote(cols: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in cols)


async def fetch_customers(
    session: AsyncSession,
    customer_ids: list[str],
    fields: list[str] | None = None,
) -> list[dict]:
    cols = _resolve_fields(fields, CUSTOMER_FIELDS, _CUSTOMER_COLUMNS)
    result = await session.execute(
        text(f"SELECT {_quote(cols)} FROM customers WHERE customer_id = ANY(:ids)"),
        {"ids": customer_ids},
    )
    return [dict(row._mapping) for row in result]


async def fetch_transactions(
    session: AsyncSession,
    customer_ids: list[str],
    fields: list[str] | None = None,
    limit_per_customer: int | None = None,
) -> list[dict]:
    cols = _resolve_fields(fields, TRANSACTION_FIELDS, _TRANSACTION_COLUMNS)
    col_expr = _quote(cols)

    if limit_per_customer is None:
        sql = f"SELECT {col_expr} FROM transactions WHERE customer_id = ANY(:ids)"
        result = await session.execute(text(sql), {"ids": customer_ids})
    else:
        # ROW_NUMBER() partitioned by customer lets us cap rows per customer
        # without a separate query per ID.
        sql = f"""
            SELECT {col_expr}
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY customer_id ORDER BY transaction_id
                       ) AS _rn
                FROM transactions
                WHERE customer_id = ANY(:ids)
            ) _ranked
            WHERE _rn <= :limit
        """
        result = await session.execute(
            text(sql), {"ids": customer_ids, "limit": limit_per_customer}
        )

    return [dict(row._mapping) for row in result]
