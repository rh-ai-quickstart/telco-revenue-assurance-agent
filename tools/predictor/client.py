"""Synchronous, batch-capable client for the data fetch service.

Set DATA_FETCH_URL to the service base URL. DATA_FETCH_TIMEOUT defaults to
30 seconds; DATA_FETCH_TOKEN optionally supplies a bearer token for a gateway.
This client uses the HTTP API without importing the service or its database.
"""

import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_BATCH_SIZE = 1000


class DataFetchError(RuntimeError):
    """The data fetch service failed or returned an invalid response."""


def _strings(
    values: Sequence[str], name: str, *, allow_empty: bool = False
) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a sequence of nonempty strings")
    if (not values and not allow_empty) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{name} must be a sequence of nonempty strings")
    return list(dict.fromkeys(values))


def _configuration() -> tuple[str, float, dict[str, str]]:
    base_url = os.environ.get("DATA_FETCH_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        parsed.port  # Validate the optional port before making a request.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Set DATA_FETCH_URL to an HTTP(S) service base URL")
    try:
        timeout = float(os.environ.get("DATA_FETCH_TIMEOUT", "30"))
    except ValueError:
        raise ValueError("DATA_FETCH_TIMEOUT must be positive seconds") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DATA_FETCH_TIMEOUT must be positive seconds")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = os.environ.get("DATA_FETCH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base_url, timeout, headers


def _fetch(
    resource: str,
    customer_ids: Sequence[str],
    fields: Sequence[str] | None,
    limit_per_customer: int | None = None,
) -> list[dict]:
    ids = _strings(customer_ids, "customer_ids")
    selected = None if fields is None else _strings(fields, "fields", allow_empty=True)
    if selected:
        selected = list(dict.fromkeys(["customer_id", *selected]))
    if limit_per_customer is not None and (
        type(limit_per_customer) is not int
        or not 1 <= limit_per_customer <= _BATCH_SIZE
    ):
        raise ValueError("limit_per_customer must be an integer from 1 to 1000")
    base_url, timeout, headers = _configuration()
    records = []
    for offset in range(0, len(ids), _BATCH_SIZE):
        batch = ids[offset : offset + _BATCH_SIZE]
        payload = {"customer_ids": batch}
        if selected:
            payload["fields"] = selected
        if limit_per_customer is not None:
            payload["limit_per_customer"] = limit_per_customer
        request = Request(
            f"{base_url}/{resource}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            error.close()
            raise DataFetchError(
                f"Data fetch /{resource} failed with HTTP {error.code}"
            ) from None
        except (URLError, OSError, HTTPException):
            raise DataFetchError(f"Data fetch /{resource} request failed") from None
        except (ValueError, UnicodeError):
            raise DataFetchError("Data fetch service returned invalid JSON") from None

        if (
            not isinstance(result, dict)
            or not isinstance(result.get("records"), list)
            or type(result.get("count")) is not int
            or result["count"] != len(result["records"])
        ):
            raise DataFetchError("Data fetch service returned invalid records/count")
        requested = set(batch)
        counts = Counter()
        for record in result["records"]:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("customer_id"), str)
                or record["customer_id"] not in requested
            ):
                raise DataFetchError(
                    "Data fetch service returned an unexpected customer"
                )
            counts[record["customer_id"]] += 1
            maximum = 1 if resource == "customers" else limit_per_customer
            if maximum is not None and counts[record["customer_id"]] > maximum:
                raise DataFetchError(
                    "Data fetch service exceeded the per-customer limit"
                )
        records.extend(result["records"])
    return records


def fetch_customer_data(
    customer_ids: Sequence[str], fields: Sequence[str] | None = None
) -> list[dict]:
    """Fetch snapshots in batches of up to 1,000 IDs; absent customers are omitted.

    Duplicate input IDs are removed. Explicit field selections always include
    customer_id so consumers can associate unordered records with their inputs.
    """
    return _fetch("customers", customer_ids, fields)


def fetch_transaction_data(
    customer_ids: Sequence[str],
    fields: Sequence[str] | None = None,
    limit_per_customer: int | None = None,
) -> list[dict]:
    """Fetch transactions; the service selects the lowest IDs when limited.

    Use fetch_latest_transaction_data for the most recent transaction per customer.
    """
    return _fetch("transactions", customer_ids, fields, limit_per_customer)


def fetch_latest_transaction_data(
    customer_ids: Sequence[str], fields: Sequence[str] | None = None
) -> list[dict]:
    """Select each customer's highest transaction ID, preserving input ID order.

    The service's limit selects the oldest transactions, so fetch all matching
    rows and select locally. Explicit fields also include transaction_id.
    Missing customers are omitted from the result.
    """
    ids = _strings(customer_ids, "customer_ids")
    selected = None if fields is None else _strings(fields, "fields", allow_empty=True)
    if selected:
        selected = [*selected, "transaction_id"]
    records = fetch_transaction_data(ids, fields=selected)
    latest = {}
    for record in records:
        transaction_id = record.get("transaction_id")
        if type(transaction_id) is not int:
            raise DataFetchError(
                "Data fetch service returned an invalid transaction_id"
            )
        customer_id = record["customer_id"]
        if (
            customer_id not in latest
            or transaction_id > latest[customer_id]["transaction_id"]
        ):
            latest[customer_id] = record
    return [latest[customer_id] for customer_id in ids if customer_id in latest]
