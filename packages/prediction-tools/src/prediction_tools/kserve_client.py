"""KServe V2 tensor adapter with strict response checks and bounded retries."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, ValidationError

from common.errors import ErrorCode, ServiceError, UpstreamError
from common.http import HttpClient, RetryPolicy

from .features import FeatureManifest, ModelKind

INFERENCE_DEADLINE_SECONDS = 30.0
INFERENCE_RETRY_DELAY_SECONDS = 0.5
_TRANSIENT_STATUSES = frozenset(range(500, 600))


@dataclass(frozen=True, slots=True)
class KServeEndpoint:
    """One configured, fixed endpoint and its secret-backed bearer token."""

    url: str
    bearer_token: SecretStr


@dataclass(frozen=True, slots=True)
class InferenceBatch:
    """Canonical typed values from one validated inference response."""

    model_version: str
    values: tuple[int | float, ...]


class KServeClientProtocol(Protocol):
    """Inference interface consumed by the prediction service."""

    async def infer(
        self,
        model_kind: ModelKind,
        manifest: FeatureManifest,
        feature_rows: Sequence[Sequence[float]],
    ) -> InferenceBatch: ...


class _KServeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    shape: list[int]
    datatype: str
    data: list[object]
    parameters: dict[str, JsonValue] | None = None


class _KServeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    outputs: list[_KServeOutput]
    parameters: dict[str, JsonValue] | None = None


class KServeV2Client:
    """Send authenticated, bounded KServe V2 inference requests."""

    def __init__(
        self,
        endpoints: Mapping[ModelKind, KServeEndpoint],
        http_client: HttpClient | None = None,
    ) -> None:
        required = {ModelKind.CHURN, ModelKind.FRAUD, ModelKind.QOE}
        if set(endpoints) != required:
            raise ValueError("Exactly one fixed endpoint is required for each model.")
        self._endpoints = dict(endpoints)
        self._http = http_client or HttpClient(
            timeout_seconds=INFERENCE_DEADLINE_SECONDS,
            retry_policy=RetryPolicy(max_retries=0),
        )
        self._owns_http = http_client is None

    async def infer(
        self,
        model_kind: ModelKind,
        manifest: FeatureManifest,
        feature_rows: Sequence[Sequence[float]],
    ) -> InferenceBatch:
        if model_kind not in self._endpoints:
            raise ValueError("Unsupported model kind.")
        payload = build_inference_request(model_kind, manifest, feature_rows)
        endpoint = self._endpoints[model_kind]
        headers = {
            "Authorization": f"Bearer {endpoint.bearer_token.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = await self._post_with_retry(endpoint.url, headers, payload)
        if response.status_code >= 400:
            if response.status_code == 400:
                raise ServiceError(
                    ErrorCode.FEATURE_SCHEMA_MISMATCH,
                    "The model rejected the validated feature tensor.",
                    retryable=False,
                )
            raise UpstreamError(
                ErrorCode.MODEL_UNAVAILABLE,
                "The requested model is temporarily unavailable.",
                retryable=response.status_code in _TRANSIENT_STATUSES,
            )
        try:
            body: object = response.json()
        except ValueError as exc:
            raise _schema_mismatch() from exc
        return parse_inference_response(model_kind, manifest, len(feature_rows), body)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _post_with_retry(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: dict[str, object],
    ) -> httpx.Response:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INFERENCE_DEADLINE_SECONDS
        for attempt in range(2):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _upstream_timeout()
            try:
                async with asyncio.timeout(remaining):
                    response = await self._http.request(
                        "POST",
                        url,
                        retryable=False,
                        timeout_seconds=remaining,
                        headers=headers,
                        json=payload,
                    )
            except (httpx.TimeoutException, TimeoutError) as exc:
                if attempt == 0 and deadline - loop.time() > INFERENCE_RETRY_DELAY_SECONDS:
                    await asyncio.sleep(INFERENCE_RETRY_DELAY_SECONDS)
                    continue
                raise _upstream_timeout() from exc
            except httpx.HTTPError as exc:
                raise UpstreamError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "The requested model is temporarily unavailable.",
                    retryable=True,
                ) from exc
            if (
                response.status_code in _TRANSIENT_STATUSES
                and attempt == 0
                and deadline - loop.time() > INFERENCE_RETRY_DELAY_SECONDS
            ):
                await asyncio.sleep(INFERENCE_RETRY_DELAY_SECONDS)
                continue
            return response
        raise _upstream_timeout()


def build_inference_request(
    model_kind: ModelKind,
    manifest: FeatureManifest,
    feature_rows: Sequence[Sequence[float]],
) -> dict[str, object]:
    """Build the exact FP32 tensor shape defined by the three inference contracts."""

    if model_kind not in {ModelKind.CHURN, ModelKind.FRAUD, ModelKind.QOE}:
        raise ValueError("Unsupported model kind.")
    if not 1 <= len(feature_rows) <= 100:
        raise _schema_mismatch()
    if manifest.feature_width != len(manifest.feature_order) or manifest.feature_width < 1:
        raise _schema_mismatch()

    normalized_rows: list[list[float]] = []
    for row in feature_rows:
        if len(row) != manifest.feature_width:
            raise _schema_mismatch()
        normalized_row: list[float] = []
        for value in row:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _schema_mismatch()
            numeric = float(value)
            if not math.isfinite(numeric):
                raise _schema_mismatch()
            normalized_row.append(numeric)
        normalized_rows.append(normalized_row)

    if model_kind == ModelKind.QOE:
        if manifest.feature_width != 8:
            raise _schema_mismatch()
        tensor_data: object = [[row] for row in normalized_rows]
        tensor_shape = [len(normalized_rows), 1, 8]
    else:
        tensor_data = normalized_rows
        tensor_shape = [len(normalized_rows), manifest.feature_width]
    tensor = {
        "name": manifest.input_tensor_name,
        "shape": tensor_shape,
        "datatype": "FP32",
        "data": tensor_data,
    }
    return {"id": f"{model_kind}-{uuid4().hex}", "inputs": [tensor]}


def parse_inference_response(
    model_kind: ModelKind,
    manifest: FeatureManifest,
    batch_size: int,
    body: object,
) -> InferenceBatch:
    """Validate model identity, tensor metadata, dimensions, and class/value mappings."""

    try:
        response = _KServeResponse.model_validate(body)
    except ValidationError as exc:
        raise _schema_mismatch() from exc
    expected_names = {
        ModelKind.CHURN: "churn-prediction",
        ModelKind.FRAUD: "fraud-prediction",
        ModelKind.QOE: "starlink-qoe",
    }
    if (
        model_kind not in expected_names
        or response.model_name != expected_names[model_kind]
        or response.model_version != manifest.model_version
        or not 1 <= batch_size <= 100
    ):
        raise _schema_mismatch()
    outputs_by_name = {output.name: output for output in response.outputs}
    if len(outputs_by_name) != len(response.outputs):
        raise _schema_mismatch()
    output = outputs_by_name.get(manifest.output_tensor_name)
    if output is None:
        raise _schema_mismatch()
    if model_kind == ModelKind.FRAUD:
        if len(response.outputs) not in (1, 2):
            raise _schema_mismatch()
        for auxiliary in response.outputs:
            if auxiliary.name != output.name:
                _validate_fraud_probabilities(auxiliary, batch_size)
    elif len(response.outputs) != 1:
        raise _schema_mismatch()
    if model_kind == ModelKind.QOE:
        valid_shape = output.shape == [batch_size, 1]
    else:
        # MLServer's NumPy codec pads one-dimensional predictions with a unit axis.
        valid_shape = output.shape in ([batch_size], [batch_size, 1])
    if not valid_shape:
        raise _schema_mismatch()
    if len(output.data) != batch_size:
        raise _schema_mismatch()

    values: list[int | float] = []
    if model_kind == ModelKind.CHURN:
        if output.datatype == "INT64":
            for value in output.data:
                if type(value) is not int:
                    raise _schema_mismatch()
                values.append(value)
        elif output.datatype in {"FP32", "FP64"}:
            # The native binary Booster returns P(class=True). This is the same
            # argmax rule as the trained LGBMClassifier, including ties at 0.5.
            for value in output.data:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _schema_mismatch()
                if not 0 <= value <= 1:
                    raise _schema_mismatch()
                probability = float(value)
                if not math.isfinite(probability):
                    raise _schema_mismatch()
                values.append(1 if probability > 0.5 else 0)
        else:
            raise _schema_mismatch()
    elif model_kind == ModelKind.FRAUD:
        if output.datatype == "BOOL":
            for value in output.data:
                if type(value) is not bool:
                    raise _schema_mismatch()
                values.append(1 if value else 0)
        elif output.datatype == "INT64":
            for value in output.data:
                if type(value) is not int or value not in (0, 1):
                    raise _schema_mismatch()
                values.append(value)
        else:
            raise _schema_mismatch()
    elif model_kind == ModelKind.QOE:
        if output.datatype not in {"FP32", "FP64"}:
            raise _schema_mismatch()
        for value in output.data:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _schema_mismatch()
            numeric = float(value)
            if not math.isfinite(numeric) or not 0 <= numeric <= 100:
                raise _schema_mismatch()
            values.append(numeric)
    else:
        raise _schema_mismatch()
    return InferenceBatch(model_version=response.model_version, values=tuple(values))


def _validate_fraud_probabilities(output: _KServeOutput, batch_size: int) -> None:
    """Validate the optional dense ONNX output without replacing its class labels."""

    if (
        output.datatype not in {"FP32", "FP64"}
        or output.shape != [batch_size, 2]
        or len(output.data) != batch_size * 2
    ):
        raise _schema_mismatch()
    for value in output.data:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1
            or not math.isfinite(value)
        ):
            raise _schema_mismatch()


def _schema_mismatch() -> ServiceError:
    return ServiceError(
        ErrorCode.FEATURE_SCHEMA_MISMATCH,
        "The model tensor or response does not match the deployed feature contract.",
        retryable=False,
    )


def _upstream_timeout() -> ServiceError:
    return UpstreamError(
        ErrorCode.UPSTREAM_TIMEOUT,
        "The model did not respond before the inference deadline.",
        retryable=True,
    )
