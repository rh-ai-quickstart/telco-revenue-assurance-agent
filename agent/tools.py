"""
Tool implementations for the OGX agent.

Each function calls an existing HTTP service and returns a result dict.
On failure returns {"error": "..."} so the LLM can reason about the problem
instead of receiving an unhandled exception.
"""
from __future__ import annotations

import os

import httpx

def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value

_PREPROCESSING_URL = _require_env("PREPROCESSING_URL")
_DATA_FETCH_URL = _require_env("DATA_FETCH_URL")
_TIMEOUT = float(os.getenv("TOOL_TIMEOUT", "30"))


def _post(url: str, payload: dict) -> dict | list:
    try:
        r = httpx.post(url, json=payload, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


def list_customers(
    limit: int = 20,
    has_churned: bool | None = None,
    region_id: int | None = None,
    has_fraud: bool | None = None,
) -> dict:
    """List real customer IDs from the database, with optional filters."""
    params: dict = {"limit": limit}
    if has_churned is not None:
        params["has_churned"] = has_churned
    if region_id is not None:
        params["region_id"] = region_id
    if has_fraud is not None:
        params["has_fraud"] = has_fraud
    try:
        r = httpx.get(f"{_DATA_FETCH_URL}/customers/sample", params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


def cross_metric_count(
    base_has_fraud: bool | None = None,
    base_predicted_churn: bool | None = None,
    cross_has_fraud: bool | None = None,
    cross_predicted_churn: bool | None = None,
    region_id: int | None = None,
) -> dict:
    """Return base count + overlap count + pre-computed overlap % in one call."""
    params: dict = {}
    if base_has_fraud is not None:
        params["base_has_fraud"] = base_has_fraud
    if base_predicted_churn is not None:
        params["base_predicted_churn"] = base_predicted_churn
    if cross_has_fraud is not None:
        params["cross_has_fraud"] = cross_has_fraud
    if cross_predicted_churn is not None:
        params["cross_predicted_churn"] = cross_predicted_churn
    if region_id is not None:
        params["region_id"] = region_id
    try:
        r = httpx.get(f"{_DATA_FETCH_URL}/customers/cross-count", params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


def count_customers(
    region_id: int | None = None,
    has_fraud: bool | None = None,
    predicted_churn: bool | None = None,
) -> dict:
    """Return a precise count of customers matching the given filters."""
    params: dict = {}
    if region_id is not None:
        params["region_id"] = region_id
    if has_fraud is not None:
        params["has_fraud"] = has_fraud
    if predicted_churn is not None:
        params["predicted_churn"] = predicted_churn
    try:
        r = httpx.get(f"{_DATA_FETCH_URL}/customers/count", params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}


def fetch_customer_data(
    customer_ids: list[str],
    fields: list[str] | None = None,
) -> dict:
    """Fetch customer profile records (billing, support, location, QoE) from the database."""
    return _post(f"{_DATA_FETCH_URL}/customers", {"customer_ids": customer_ids, "fields": fields})


def fetch_transaction_data(
    customer_ids: list[str],
    fields: list[str] | None = None,
    limit_per_customer: int | None = None,
) -> dict:
    """Fetch transaction records for one or more customers from the database."""
    payload: dict = {"customer_ids": customer_ids}
    if fields is not None:
        payload["fields"] = fields
    if limit_per_customer is not None:
        payload["limit_per_customer"] = limit_per_customer
    return _post(f"{_DATA_FETCH_URL}/transactions", payload)


def predict_churn(customer_ids: list[str]) -> dict | list:
    """Score customers for churn risk. Returns label (0=safe, 1=at-risk), verdict, and churn_probability."""
    return _post(f"{_PREPROCESSING_URL}/predict/churn/batch", {"customer_ids": customer_ids})


def predict_fraud(customer_ids: list[str]) -> dict | list:
    """Detect fraud risk from transaction patterns. Returns label (0=clean, 1=fraud) and verdict."""
    return _post(f"{_PREPROCESSING_URL}/predict/fraud/batch", {"customer_ids": customer_ids})


def predict_qoe(customer_ids: list[str]) -> dict | list:
    """Assess network Quality of Experience. Returns qoe_score, verdict, download_mbps, upload_mbps, latency_ms per customer."""
    result = _post(f"{_PREPROCESSING_URL}/predict/qoe/batch", {"customer_ids": customer_ids})
    if isinstance(result, dict) and "error" not in result and "predictions" in result:
        try:
            network = _post(f"{_DATA_FETCH_URL}/customers", {
                "customer_ids": customer_ids,
                "fields": ["customer_id", "qoe_download_mbps", "qoe_upload_mbps", "qoe_latency_ms"],
            })
            if isinstance(network, dict) and "records" in network:
                by_id = {r["customer_id"]: r for r in network["records"]}
                for pred in result["predictions"]:
                    m = by_id.get(pred.get("customer_id"), {})
                    pred["download_mbps"] = round(m.get("qoe_download_mbps") or 0, 1)
                    pred["upload_mbps"]   = round(m.get("qoe_upload_mbps") or 0, 1)
                    pred["latency_ms"]    = round(m.get("qoe_latency_ms") or 0, 1)
        except Exception:
            pass  # enrichment is best-effort
    return result


def query_db(group_by: str = "region_id") -> dict:
    """Query aggregated customer stats grouped by region. Returns churn rate, avg QoE score, and revenue at risk per group."""
    try:
        r = httpx.get(f"{_DATA_FETCH_URL}/customers/grouped", params={"group_by": group_by}, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"error": str(exc)}
