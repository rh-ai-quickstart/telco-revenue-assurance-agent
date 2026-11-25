"""Customer-ID tools. Serving protocol, URLs, and caches stay inside this module."""

import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from threading import Condition, RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cachetools import TTLCache, cached

from .batching import PredictionWorkers
from .client import DataFetchError, fetch_customer_data, fetch_latest_transaction_data
from .features import (
    CHURN_DB_FIELDS,
    FRAUD_DB_FIELDS,
    QOE_FEATURES,
    PreprocessingError,
    preprocess_churn,
    preprocess_fraud,
    preprocess_qoe,
)


class InferenceError(RuntimeError):
    """The serving backend failed or returned an invalid inference response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _PredictionCache:
    def __init__(self, ttl: float, maxsize: int) -> None:
        self.ttl = ttl
        self.maxsize = maxsize
        self.context = None
        self.lock = RLock()
        self._memoized = None
        self._uncached_misses = 0

    def _new_memoized(self) -> Callable[[str, Callable[[], dict]], dict]:
        @cached(
            cache=TTLCache(maxsize=self.maxsize, ttl=self.ttl, timer=time.monotonic),
            key=lambda key, compute: key,
            condition=Condition(),
            info=True,
        )
        def infer(key: str, compute: Callable[[], dict]) -> dict:
            return dict(compute())

        return infer

    def predict(
        self, features: list[float], context: tuple, compute: Callable[[], dict]
    ) -> dict:
        key = hashlib.sha256(
            json.dumps(features, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self.lock:
            if self._memoized is None or self.context != context:
                self._memoized = self._new_memoized()
                self.context = context
                self._uncached_misses = 0
            memoized = self._memoized
            if self.ttl == 0:
                self._uncached_misses += 1
        if self.ttl == 0:
            return dict(compute())
        return dict(memoized(key, compute))

    def stats(self) -> dict:
        with self.lock:
            info = self._memoized.cache_info() if self._memoized else None
            return {
                "cache_hits": info.hits if info else 0,
                "cache_misses": (info.misses if info else 0) + self._uncached_misses,
                "cache_size": info.currsize if info else 0,
            }


_caches = {}
_cache_lock = RLock()
_cache_settings = None
_workers = None
_workers_lock = RLock()
_inference_stats = {}
_stats_lock = RLock()
_request_token = ContextVar("prediction_request_token", default=None)
_inference_log = logging.getLogger("uvicorn.error")


def clear_prediction_caches() -> None:
    """Clear process/session inference results, e.g. after redeploying a model."""
    global _cache_settings
    with _cache_lock:
        _caches.clear()
        _cache_settings = None
    with _stats_lock:
        _inference_stats.clear()


def shutdown_prediction_workers() -> None:
    """Drain inference/cache workers at service shutdown; later calls can restart."""
    global _workers
    with _workers_lock:
        if _workers is not None:
            _workers.shutdown()
            _workers = None


def prediction_stats() -> dict:
    """Return counters only, without feature values, customer IDs, tokens or URLs."""
    with _cache_lock:
        caches = dict(_caches)
    with _stats_lock:
        stats = {
            model: {**values, "batch_sizes": dict(values["batch_sizes"])}
            for model, values in _inference_stats.items()
        }
    result = {}
    for model in ("churn", "fraud", "qoe"):
        result[model] = {
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_size": 0,
            "inference_requests": 0,
            "inference_rows": 0,
            "batch_sizes": {},
            "last_batch_size": 0,
            "max_batch_size": 0,
            **stats.get(model, {}),
        }
        if model in caches:
            result[model].update(caches[model].stats())
    return result


def _call_infer(model, endpoint, input_name, features, shape, token):
    with _stats_lock:
        stats = _inference_stats.setdefault(
            model,
            {
                "inference_requests": 0,
                "inference_rows": 0,
                "batch_sizes": {},
                "last_batch_size": 0,
                "max_batch_size": 0,
            },
        )
        size = shape[0]
        stats["inference_requests"] += 1
        stats["inference_rows"] += size
        stats["batch_sizes"][size] = stats["batch_sizes"].get(size, 0) + 1
        stats["last_batch_size"] = size
        stats["max_batch_size"] = max(size, stats["max_batch_size"])
    reset = _request_token.set(token)
    started_at = time.monotonic()
    target = urlsplit(endpoint).netloc
    _inference_log.info(
        "kserve_call_started model=%s target=%s batch_size=%d",
        model,
        target,
        size,
    )
    try:
        result = _infer(endpoint, input_name, features, shape)
    except Exception as error:
        status_code = getattr(error, "status_code", None)
        _inference_log.warning(
            "kserve_call_failed model=%s target=%s status=%s elapsed_ms=%.1f error_type=%s",
            model,
            target,
            status_code if status_code is not None else "unavailable",
            (time.monotonic() - started_at) * 1000,
            type(error).__name__,
        )
        raise
    else:
        _inference_log.info(
            "kserve_call_completed model=%s target=%s outcome=response_received elapsed_ms=%.1f",
            model,
            target,
            (time.monotonic() - started_at) * 1000,
        )
        return result
    finally:
        _request_token.reset(reset)


def _cache(model: str) -> _PredictionCache:
    global _cache_settings
    try:
        ttl = float(os.environ.get("CACHE_TTL", "300"))
        maxsize = int(os.environ.get("CACHE_MAXSIZE", "512"))
    except ValueError:
        raise ValueError(
            "CACHE_TTL must be numeric and CACHE_MAXSIZE must be an integer"
        ) from None
    if not math.isfinite(ttl) or ttl < 0 or maxsize < 1:
        raise ValueError(
            "CACHE_TTL must be finite and nonnegative; CACHE_MAXSIZE must be positive"
        )
    with _cache_lock:
        if _cache_settings != (ttl, maxsize):
            _caches.clear()
            _cache_settings = (ttl, maxsize)
        if model not in _caches:
            _caches[model] = _PredictionCache(ttl, maxsize)
        return _caches[model]


def _endpoint(model: str) -> str:
    variable = f"{model.upper()}_INFER_URL"
    url = os.environ.get(variable, "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            f"Set {variable} to an HTTP(S) inference URL without embedded credentials"
        )
    return url


def _infer_token() -> str:
    """Read a rotating service-account token on each prediction when configured."""
    path = os.environ.get("INFER_TOKEN_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as source:
                token = source.read().strip()
        except (OSError, UnicodeError):
            raise ValueError("INFER_TOKEN_FILE could not be read") from None
        if not token:
            raise ValueError("INFER_TOKEN_FILE must contain a nonempty token")
        return token
    return os.environ.get("INFER_TOKEN", "").strip()


def _infer(
    endpoint_url: str, input_name: str, features: list[float], shape: list[int]
) -> dict:
    """The single transport boundary to replace when changing serving backend."""
    try:
        timeout = float(os.environ.get("INFER_TIMEOUT", "30"))
    except ValueError:
        raise ValueError("INFER_TIMEOUT must be a positive number of seconds") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("INFER_TIMEOUT must be a positive number of seconds")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = _request_token.get()
    if token is None:
        token = _infer_token()
    token = token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "inputs": [
            {"name": input_name, "shape": shape, "datatype": "FP32", "data": features}
        ]
    }
    request = Request(
        endpoint_url,
        data=json.dumps(payload, allow_nan=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as error:
        status_code = error.code
        error.close()
        raise InferenceError(
            f"Inference request failed with HTTP {status_code}",
            status_code=status_code,
        ) from None
    except (URLError, TimeoutError, OSError):
        raise InferenceError("Inference endpoint could not be reached") from None
    except (ValueError, UnicodeError):
        raise InferenceError("Inference endpoint returned invalid JSON") from None
    return result


def _batch_output(
    response: dict, names: tuple[str, ...], datatypes: tuple[str, ...], count: int
):
    if not isinstance(response, dict) or not isinstance(response.get("outputs"), list):
        raise InferenceError("Inference response must contain a v2 outputs list")
    outputs = {}
    for output in response["outputs"]:
        if not isinstance(output, dict) or not isinstance(output.get("name"), str):
            raise InferenceError("Invalid inference output descriptor")
        if output["name"] in outputs:
            raise InferenceError("Duplicate inference output name")
        outputs[output["name"]] = output
    output = next((outputs[name] for name in names if name in outputs), None)
    if output is None:
        raise InferenceError(
            f"Inference response is missing output {' or '.join(names)}"
        )
    if output.get("datatype") not in datatypes:
        raise InferenceError("Inference output has an unexpected datatype")
    shape = output.get("shape")
    if (
        not isinstance(shape, list)
        or any(type(n) is not int for n in shape)
        or shape not in ([count], [count, 1])
    ):
        raise InferenceError(
            f"Inference output shape must contain one value for each of {count} rows"
        )
    data = output.get("data")
    if not isinstance(data, list):
        raise InferenceError("Inference output must contain tensor data")
    if (
        shape == [count, 1]
        and len(data) == count
        and all(isinstance(row, list) for row in data)
    ):
        if any(len(row) != 1 for row in data):
            raise InferenceError(
                "Inference output tensor data does not match its shape"
            )
        data = [row[0] for row in data]
    if len(data) != count:
        raise InferenceError("Inference output tensor data does not match its shape")
    for value in data:
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise InferenceError("Inference output must contain finite numeric values")
    return data


def _scalar_output(response: dict, names: tuple[str, ...], datatypes: tuple[str, ...]):
    return _batch_output(response, names, datatypes, 1)[0]


def _fetch_record(customer_id: str, *, domain: str) -> dict:
    """Fetch one model's source record through the shared data fetch service."""
    if domain == "fraud":
        records = fetch_latest_transaction_data(
            [customer_id], fields=[*FRAUD_DB_FIELDS, "plan_type"]
        )
    elif domain == "churn":
        records = fetch_customer_data([customer_id], fields=list(CHURN_DB_FIELDS))
    else:
        # The current service exposes only part of the required QoE inputs.
        # Fetch its snapshot and report missing fields rather than requesting
        # columns its allowlist rejects.
        records = fetch_customer_data([customer_id])
    if not records:
        raise LookupError(f"No {domain} data found for customer {customer_id!r}")
    record = records[0]
    if domain == "qoe":
        required = (*QOE_FEATURES[:6], "season", "weather")
        missing = [field for field in required if record.get(field) is None]
        if missing:
            raise PreprocessingError(
                "Data fetch tool returned incomplete QoE data; missing fields: "
                + ", ".join(missing)
            )
    return record


def _predict(
    model: str,
    customer_id: str,
    preprocess,
    input_name: str,
    shape: list[int],
    parse,
    policy: tuple = (),
) -> dict:
    if not isinstance(customer_id, str) or not customer_id.strip():
        raise ValueError("customer_id must be a nonempty string")
    endpoint = _endpoint(model)
    cache = _cache(model)
    context = (endpoint, _infer_token(), policy)
    # Always fetch current data, even on a cache hit; feature changes invalidate the key.
    features = preprocess(_fetch_record(customer_id, domain=model))
    return cache.predict(
        features,
        context,
        lambda: parse(
            _call_infer(model, endpoint, input_name, features, shape, context[1])
        ),
    )


def predict_churn(customer_id: str) -> dict:
    """Predict churn for a customer's latest billing cycle."""

    def parse(response):
        probability = float(_scalar_output(response, ("predict",), ("FP64",)))
        if not 0 <= probability <= 1:
            raise InferenceError("Churn probability must be between 0 and 1")
        label = int(probability >= 0.5)
        return {
            "label": label,
            "verdict": "Will Churn" if label else "Will Not Churn",
            "churn_probability": probability,
        }

    return _predict("churn", customer_id, preprocess_churn, "predict", [1, 14], parse)


def predict_fraud(customer_id: str) -> dict:
    """Predict fraud using the customer's most recent transaction."""

    def parse(response):
        # MLServer exposes predict; exported ONNX models commonly expose label.
        label = _scalar_output(response, ("predict", "label"), ("INT64",))
        if type(label) is not int or label not in (0, 1):
            raise InferenceError("Fraud label must be an INT64 value of 0 or 1")
        return {"label": label, "verdict": "Fraud" if label else "Non-Fraud"}

    return _predict(
        "fraud", customer_id, preprocess_fraud, "float_input", [1, 12], parse
    )


def predict_qoe(customer_id: str) -> dict:
    """Predict QoE and classify the score as Poor (0), Fair (1), or Good (2)."""
    poor, good = _qoe_policy()

    def parse(response):
        # MLServer exposes predict; the ONNX runtime can expose output directly.
        score = float(_scalar_output(response, ("predict", "output"), ("FP32", "FP64")))
        # A regressor can extrapolate beyond 0–100; the outer classes cover those scores.
        qoe_class = 0 if score < poor else 1 if score < good else 2
        return {"qoe_score": round(score, 2), "qoe_class": qoe_class, "verdict": ("Poor", "Fair", "Good")[qoe_class]}

    return _predict(
        "qoe", customer_id, preprocess_qoe, "input", [1, 1, 8], parse, (poor, good)
    )


def _qoe_policy() -> tuple[float, float]:
    try:
        poor = float(os.environ.get("QOE_POOR_THRESHOLD", "40"))
        good = float(os.environ.get("QOE_GOOD_THRESHOLD", "70"))
    except ValueError:
        raise ValueError("QoE thresholds must be numeric scores") from None
    if not (0 <= poor < good <= 100):
        raise ValueError(
            "QoE thresholds must satisfy 0 <= QOE_POOR_THRESHOLD < QOE_GOOD_THRESHOLD <= 100"
        )
    return poor, good


def _parse_batch(model: str, response: dict, count: int, policy: tuple) -> list[dict]:
    """Validate every output before allowing any row to enter the shared cache."""
    if model == "churn":
        values = _batch_output(response, ("predict",), ("FP64",), count)
        if any(not 0 <= value <= 1 for value in values):
            raise InferenceError("Churn probability must be between 0 and 1")
        return [
            {
                "label": int(value >= 0.5),
                "verdict": "Will Churn" if value >= 0.5 else "Will Not Churn",
                "churn_probability": float(value),
            }
            for value in values
        ]
    if model == "fraud":
        values = _batch_output(response, ("predict", "label"), ("INT64",), count)
        if any(type(value) is not int or value not in (0, 1) for value in values):
            raise InferenceError("Fraud label must be an INT64 value of 0 or 1")
        return [
            {"label": value, "verdict": "Fraud" if value else "Non-Fraud"}
            for value in values
        ]
    values = _batch_output(response, ("predict", "output"), ("FP32", "FP64"), count)
    poor, good = policy
    classes = [0 if value < poor else 1 if value < good else 2 for value in values]
    return [
        {"qoe_score": round(score, 2), "qoe_class": cls, "verdict": ("Poor", "Fair", "Good")[cls]}
        for score, cls in zip(values, classes)
    ]


def _batch_configuration() -> tuple[int, float]:
    try:
        size = int(os.environ.get("BATCH_MAX_SIZE", "32"))
        wait_ms = float(os.environ.get("BATCH_WAIT_MS", "10"))
    except ValueError:
        raise ValueError(
            "BATCH_MAX_SIZE must be an integer and BATCH_WAIT_MS must be numeric"
        ) from None
    if not 1 <= size <= 1000:
        raise ValueError("BATCH_MAX_SIZE must be between 1 and 1000")
    if not math.isfinite(wait_ms) or not 0 <= wait_ms <= 1000:
        raise ValueError("BATCH_WAIT_MS must be between 0 and 1000 milliseconds")
    return size, wait_ms / 1000


def _fetch_records(model: str, customer_ids: list[str]) -> dict[str, dict]:
    if model == "fraud":
        records = fetch_latest_transaction_data(
            customer_ids, fields=[*FRAUD_DB_FIELDS, "plan_type"]
        )
    elif model == "churn":
        records = fetch_customer_data(customer_ids, fields=list(CHURN_DB_FIELDS))
    else:
        records = fetch_customer_data(customer_ids)
    requested = set(customer_ids)
    by_id = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("customer_id"), str
        ):
            raise DataFetchError(
                "Data fetch service returned an invalid customer record"
            )
        customer_id = record["customer_id"]
        if customer_id not in requested or customer_id in by_id:
            raise DataFetchError(
                "Data fetch service returned an unexpected customer record"
            )
        by_id[customer_id] = record
    missing = [customer_id for customer_id in customer_ids if customer_id not in by_id]
    if missing:
        raise LookupError(f"No {model} data found for customer {missing[0]!r}")
    if model == "qoe":
        required = (*QOE_FEATURES[:6], "season", "weather")
        for record in records:
            absent = [field for field in required if record.get(field) is None]
            if absent:
                raise PreprocessingError(
                    "Data fetch tool returned incomplete QoE data; missing fields: "
                    + ", ".join(absent)
                )
    return by_id


def predict_batch(model: str, customer_ids: list[str]) -> list[dict]:
    """Fetch current inputs and predict in true tensors, preserving IDs' order.

    Repeated IDs and identical feature vectors share work. Cache misses from
    concurrent requests can coalesce during BATCH_WAIT_MS. BATCH_MAX_SIZE caps
    each model request. The shared cache also serves the singleton Python tools.
    """
    global _workers
    if not isinstance(model, str) or model not in ("churn", "fraud", "qoe"):
        raise ValueError("model must be one of: churn, fraud, qoe")
    if (
        not isinstance(customer_ids, list)
        or not customer_ids
        or any(
            not isinstance(customer_id, str) or not customer_id.strip()
            for customer_id in customer_ids
        )
    ):
        raise ValueError("customer_ids must be a nonempty list of nonempty strings")
    size, wait = _batch_configuration()
    endpoint, cache = _endpoint(model), _cache(model)
    policy = _qoe_policy() if model == "qoe" else ()
    context = (endpoint, _infer_token(), policy)
    preprocess, input_name, tail = {
        "churn": (preprocess_churn, "predict", [14]),
        "fraud": (preprocess_fraud, "float_input", [12]),
        "qoe": (preprocess_qoe, "input", [1, 8]),
    }[model]
    ids = list(dict.fromkeys(customer_ids))
    records = _fetch_records(model, ids)
    # Complete source validation and preprocessing before scheduling any model work.
    features = {customer_id: preprocess(records[customer_id]) for customer_id in ids}
    keys = {
        customer_id: json.dumps(row, sort_keys=True)
        for customer_id, row in features.items()
    }
    unique = {keys[customer_id]: row for customer_id, row in features.items()}

    def run_batch(rows):
        response = _call_infer(
            model,
            endpoint,
            input_name,
            [value for row in rows for value in row],
            [len(rows), *tail],
            context[1],
        )
        return _parse_batch(model, response, len(rows), policy)

    with _workers_lock:
        if _workers is None:
            _workers = PredictionWorkers()
        workers = _workers
        futures = {
            key: workers.rows.submit(
                cache.predict,
                row,
                context,
                lambda row=row: workers.infer(
                    (model, context), row, run_batch, size, wait
                ),
            )
            for key, row in unique.items()
        }
    results = {}
    failure = None
    # Drain every task on failure, so no work escapes the lifetime of the call.
    for key, future in futures.items():
        try:
            results[key] = future.result()
        except Exception as error:  # noqa: BLE001 - Drain all futures, then re-raise unchanged.
            if failure is None:
                failure = error
    if failure is not None:
        raise failure
    return [dict(results[keys[customer_id]]) for customer_id in customer_ids]
