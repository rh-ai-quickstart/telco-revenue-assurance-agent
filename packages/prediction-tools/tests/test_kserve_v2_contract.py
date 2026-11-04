"""Frozen KServe V2 tensor and model mapping contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast

import httpx
import numpy as np
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from prediction_tools.features import (
    QOE_FEATURE_ORDER,
    FraudFeatureManifest,
    ModelFeatureManifest,
    ModelKind,
    QoeFeatureManifest,
    QoePreprocessor,
    Transformer,
    transform_fraud_features,
    transform_qoe_features,
)
from prediction_tools.kserve_client import (
    KServeEndpoint,
    KServeV2Client,
    build_inference_request,
    parse_inference_response,
)
from pydantic import SecretStr

from common.errors import ErrorCode, ServiceError
from common.http import HttpClient, RetryPolicy


def _churn_manifest() -> ModelFeatureManifest:
    return ModelFeatureManifest(
        model_name="churn-prediction",
        model_version="churn-artifact-digest",
        input_tensor_name="predict",
        output_tensor_name="predict",
        feature_order=["PaymentStatus", "account_balance"],
        feature_width=2,
    )


def _fraud_manifest() -> FraudFeatureManifest:
    return FraudFeatureManifest(
        model_name="fraud-prediction",
        model_version="fraud-artifact-digest",
        input_tensor_name="float_input",
        output_tensor_name="fraud_class",
        feature_order=["amount", "Plan_Type_Pro", "Plan_Type_Premium"],
        feature_width=3,
        plan_type_categories=["Basic", "Pro", "Premium"],
        class_order=[False, True],
    )


def _qoe_manifest() -> QoeFeatureManifest:
    return QoeFeatureManifest(
        model_name="starlink-qoe",
        model_version="qoe-artifact-digest",
        input_tensor_name="input",
        output_tensor_name="output",
        feature_order=list(QOE_FEATURE_ORDER),
        feature_width=8,
        scaler_file="scaler.joblib",
        label_encoders_file="label_encoders.joblib",
    )


def _contract_accepts(model_kind: ModelKind, payload: object, *, response: bool = False) -> bool:
    contract_path = Path(__file__).parents[3] / "contracts" / f"{model_kind}-inference.json"
    schema = cast(dict[str, object], json.loads(contract_path.read_text(encoding="utf-8")))
    Draft202012Validator.check_schema(schema)
    if response:
        schema["$ref"] = "#/$defs/InferenceResponse"
    return not any(Draft202012Validator(schema).iter_errors(payload))


def _fraud_class_output() -> dict[str, object]:
    # The real class-output name is deployment metadata; this name is a test fixture.
    return {"name": "fraud_class", "shape": [2], "datatype": "INT64", "data": [0, 1]}


def _fraud_probability_output() -> dict[str, object]:
    return {
        "name": "fraud_probabilities",
        "shape": [2, 2],
        "datatype": "FP32",
        "data": [0.1, 0.9, 0.8, 0.2],
    }


def _response(
    manifest: ModelFeatureManifest, outputs: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "id": "response-fixture",
        "model_name": manifest.model_name,
        "model_version": manifest.model_version,
        "outputs": outputs,
    }


def test_churn_uses_manifest_order_and_batch_feature_shape() -> None:
    manifest = _churn_manifest()

    payload = build_inference_request(
        ModelKind.CHURN,
        manifest,
        [(0.0, 13.5), (1.0, 21.25)],
    )

    assert payload["inputs"] == [
        {
            "name": "predict",
            "shape": [2, 2],
            "datatype": "FP32",
            "data": [[0.0, 13.5], [1.0, 21.25]],
        }
    ]
    parsed = parse_inference_response(
        ModelKind.CHURN,
        manifest,
        2,
        {
            "id": "churn-response-1",
            "model_name": "churn-prediction",
            "model_version": "churn-artifact-digest",
            "outputs": [
                {
                    "name": "predict",
                    "shape": [2],
                    "datatype": "INT64",
                    "data": [0, 1],
                }
            ],
        },
    )
    assert parsed.values == (0, 1)
    assert parsed.model_version == "churn-artifact-digest"


def test_fraud_drop_first_order_and_positive_class_mapping() -> None:
    manifest = _fraud_manifest()
    row = transform_fraud_features(
        manifest,
        {"amount": 45.0, "Plan_Type": "Premium"},
    )

    assert row == (45.0, 0.0, 1.0)
    payload = build_inference_request(ModelKind.FRAUD, manifest, [row])
    assert payload["inputs"] == [
        {
            "name": "float_input",
            "shape": [1, 3],
            "datatype": "FP32",
            "data": [[45.0, 0.0, 1.0]],
        }
    ]
    assert _contract_accepts(ModelKind.FRAUD, payload)

    for raw_value, expected_class in ((False, 0), (True, 1)):
        result = parse_inference_response(
            ModelKind.FRAUD,
            manifest,
            1,
            {
                "id": "fraud-response-1",
                "model_name": "fraud-prediction",
                "model_version": "fraud-artifact-digest",
                "outputs": [
                    {
                        "name": "fraud_class",
                        "shape": [1],
                        "datatype": "BOOL",
                        "data": [raw_value],
                    }
                ],
            },
        )
        assert result.values == (expected_class,)
        assert result.model_version == "fraud-artifact-digest"


def test_qoe_encodes_and_scales_all_eight_features_in_contract_order() -> None:
    class FakeLabelEncoder:
        def __init__(self, values: dict[str, int]) -> None:
            self._values = values

        def transform(self, values: object, /) -> object:
            labels = np.asarray(values, dtype=object)
            return np.asarray([self._values[str(value)] for value in labels])

    class FakeScaler:
        def transform(self, values: object, /) -> object:
            return np.asarray(values, dtype=np.float64) + 1.0

    manifest = _qoe_manifest()
    preprocessor = QoePreprocessor(
        cast(Transformer, FakeScaler()),
        {
            "season": cast(Transformer, FakeLabelEncoder({"winter": 2})),
            "weather": cast(Transformer, FakeLabelEncoder({"clear": 3})),
        },
    )
    features: dict[str, object] = {
        "latitude": 1.0,
        "longitude": 2.0,
        "elevation_m": 3.0,
        "visible_satellites": 4.0,
        "serving_satellites": 5.0,
        "signal_loss_db": 6.0,
        "season": "winter",
        "weather": "clear",
    }

    row = transform_qoe_features(manifest, features, preprocessor)
    payload = build_inference_request(ModelKind.QOE, manifest, [row])

    assert row == (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 3.0, 4.0)
    assert payload["inputs"] == [
        {
            "name": "input",
            "shape": [1, 1, 8],
            "datatype": "FP32",
            "data": [[[2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 3.0, 4.0]]],
        }
    ]
    assert _contract_accepts(ModelKind.QOE, payload)


def test_qoe_source_output_shape_matches_frozen_contract() -> None:
    manifest = _qoe_manifest()
    response = _response(
        manifest,
        [{"name": "output", "shape": [2, 1], "datatype": "FP32", "data": [72.5, 86.0]}],
    )

    assert _contract_accepts(ModelKind.QOE, response, response=True)
    result = parse_inference_response(ModelKind.QOE, manifest, 2, response)
    assert result.values == (72.5, 86.0)
    assert result.model_version == manifest.model_version


@pytest.mark.parametrize("probabilities_first", [False, True])
@pytest.mark.parametrize("datatype", ["INT64", "BOOL"])
def test_fraud_selects_class_output_by_name_without_using_probabilities(
    probabilities_first: bool, datatype: str
) -> None:
    manifest = _fraud_manifest()
    class_output = _fraud_class_output()
    class_output["datatype"] = datatype
    if datatype == "BOOL":
        class_output["data"] = [False, True]
    outputs = [class_output, _fraud_probability_output()]
    if probabilities_first:
        outputs.reverse()
    response = _response(manifest, outputs)

    assert _contract_accepts(ModelKind.FRAUD, response, response=True)
    result = parse_inference_response(ModelKind.FRAUD, manifest, 2, response)
    # Deliberately disagreeing probability maxima cannot override model class labels.
    assert result.values == (0, 1)
    assert result.model_version == manifest.model_version


@pytest.mark.parametrize(
    ("shape", "data"),
    [([2], [72.5, 86.0]), ([2, 2], [72.5, 86.0]), ([2, 1], [[72.5], [86.0]])],
)
def test_qoe_rejects_wrong_shape_and_nonflat_data(shape: list[int], data: list[object]) -> None:
    manifest = _qoe_manifest()
    response = _response(
        manifest,
        [{"name": "output", "shape": shape, "datatype": "FP32", "data": data}],
    )

    assert not _contract_accepts(ModelKind.QOE, response, response=True)
    with pytest.raises(ServiceError) as error:
        parse_inference_response(ModelKind.QOE, manifest, 2, response)
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0, 101.0, True, "80"])
def test_qoe_rejects_nonfinite_or_invalid_scores(value: object) -> None:
    manifest = _qoe_manifest()
    response = _response(
        manifest,
        [{"name": "output", "shape": [1, 1], "datatype": "FP32", "data": [value]}],
    )

    with pytest.raises(ServiceError) as error:
        parse_inference_response(ModelKind.QOE, manifest, 1, response)
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


def test_qoe_checks_flat_data_length_against_batch_beyond_json_schema() -> None:
    manifest = _qoe_manifest()
    response = _response(
        manifest,
        [{"name": "output", "shape": [2, 1], "datatype": "FP32", "data": [72.5]}],
    )

    assert _contract_accepts(ModelKind.QOE, response, response=True)
    with pytest.raises(ServiceError) as error:
        parse_inference_response(ModelKind.QOE, manifest, 2, response)
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize(
    "outputs",
    [
        [],
        [_fraud_probability_output()],
        [_fraud_class_output(), _fraud_class_output()],
        [_fraud_class_output(), dict(_fraud_probability_output(), name="fraud_class")],
        [dict(_fraud_class_output(), name="missing-class")],
        [_fraud_class_output(), dict(_fraud_class_output(), name="second-class")],
        [_fraud_class_output(), dict(_fraud_probability_output(), shape=[1, 2])],
        [_fraud_class_output(), dict(_fraud_probability_output(), shape=[2, 3])],
        [_fraud_class_output(), dict(_fraud_probability_output(), data=[0.1, 0.9])],
        [_fraud_class_output(), dict(_fraud_probability_output(), data=[0.1, 0.9, 0.2, 1.1])],
        [
            _fraud_class_output(),
            dict(_fraud_probability_output(), data=[0.1, 0.9, 0.2, float("nan")]),
        ],
        [_fraud_class_output(), dict(_fraud_probability_output(), data=[0.1, 0.9, 0.2, True])],
        [dict(_fraud_class_output(), shape=[2, 2])],
        [dict(_fraud_class_output(), data=[0])],
        [dict(_fraud_class_output(), data=[0, 2])],
        [dict(_fraud_class_output(), data=[False, True])],
        [dict(_fraud_class_output(), datatype="BOOL", data=[0, 1])],
    ],
)
def test_fraud_rejects_invalid_or_ambiguous_outputs(outputs: list[dict[str, object]]) -> None:
    with pytest.raises(ServiceError) as error:
        parse_inference_response(
            ModelKind.FRAUD, _fraud_manifest(), 2, _response(_fraud_manifest(), outputs)
        )
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("model_kind", [ModelKind.FRAUD, ModelKind.QOE])
@pytest.mark.parametrize(
    ("field", "value"),
    [("model_name", "different-model"), ("model_version", "different-artifact")],
)
def test_source_shaped_responses_still_require_model_identity(
    model_kind: ModelKind, field: str, value: str
) -> None:
    if model_kind == ModelKind.FRAUD:
        manifest: ModelFeatureManifest = _fraud_manifest()
        outputs = [_fraud_class_output(), _fraud_probability_output()]
    else:
        manifest = _qoe_manifest()
        outputs = [{"name": "output", "shape": [2, 1], "datatype": "FP32", "data": [72.5, 86.0]}]
    response = _response(manifest, outputs)
    response[field] = value

    with pytest.raises(ServiceError) as error:
        parse_inference_response(model_kind, manifest, 2, response)
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


def test_width_and_response_tensor_mismatches_are_rejected() -> None:
    manifest = _churn_manifest()

    with pytest.raises(ServiceError) as width_error:
        build_inference_request(ModelKind.CHURN, manifest, [(1.0,)])
    assert width_error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH

    with pytest.raises(ServiceError) as response_error:
        parse_inference_response(
            ModelKind.CHURN,
            manifest,
            1,
            {
                "id": "churn-response-1",
                "model_name": "churn-prediction",
                "model_version": "churn-artifact-digest",
                "outputs": [
                    {
                        "name": "wrong-output",
                        "shape": [1],
                        "datatype": "INT64",
                        "data": [1],
                    }
                ],
            },
        )
    assert response_error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH

    with pytest.raises(ServiceError) as version_error:
        parse_inference_response(
            ModelKind.CHURN,
            manifest,
            1,
            {
                "id": "churn-response-1",
                "model_name": "churn-prediction",
                "model_version": "different-serving-artifact",
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1],
                        "datatype": "INT64",
                        "data": [1],
                    }
                ],
            },
        )
    assert version_error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.asyncio
async def test_authenticated_inference_retries_one_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "churn-response-1",
                "model_name": "churn-prediction",
                "model_version": "churn-artifact-digest",
                "outputs": [
                    {
                        "name": "predict",
                        "shape": [1],
                        "datatype": "INT64",
                        "data": [1],
                    }
                ],
            },
        )

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shared_http = HttpClient(
        timeout_seconds=1.0,
        retry_policy=RetryPolicy(max_retries=0),
        client=async_client,
    )
    endpoints = {
        ModelKind.CHURN: KServeEndpoint(
            url="https://churn.internal/v2/models/churn-prediction/infer",
            bearer_token=SecretStr("churn-test-token"),
        ),
        ModelKind.FRAUD: KServeEndpoint(
            url="https://fraud.internal/v2/models/fraud-prediction/infer",
            bearer_token=SecretStr("fraud-test-token"),
        ),
        ModelKind.QOE: KServeEndpoint(
            url="https://qoe.internal/v2/models/starlink-qoe/infer",
            bearer_token=SecretStr("qoe-test-token"),
        ),
    }
    client = KServeV2Client(endpoints, shared_http)
    try:
        result = await client.infer(ModelKind.CHURN, _churn_manifest(), [(0.0, 5.0)])
    finally:
        await shared_http.aclose()
        await async_client.aclose()

    assert len(attempts) == 2
    assert attempts[0].headers["Authorization"] == "Bearer churn-test-token"
    assert result.values == (1,)


@pytest.mark.parametrize("datatype", ["FP32", "FP64"])
@pytest.mark.parametrize("shape", [[5], [5, 1]])
def test_churn_native_probabilities_follow_trained_binary_classifier(
    datatype: str, shape: list[int]
) -> None:
    manifest = _churn_manifest()
    response = _response(
        manifest,
        [
            {
                "name": "predict",
                "shape": shape,
                "datatype": datatype,
                "data": [0.0, 0.499999, 0.5, 0.500001, 1.0],
                "parameters": {"content_type": "np"},
            }
        ],
    )
    assert _contract_accepts(ModelKind.CHURN, response, response=True)
    result = parse_inference_response(ModelKind.CHURN, manifest, 5, response)
    assert result.values == (0, 0, 0, 1, 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01, True, "0.5", 10**400])
def test_churn_rejects_invalid_native_probability(value: object) -> None:
    manifest = _churn_manifest()
    response = _response(
        manifest,
        [
            {
                "name": "predict",
                "shape": [1],
                "datatype": "FP64",
                "data": [value],
                "parameters": {"content_type": "np"},
            }
        ],
    )
    with pytest.raises(ServiceError) as error:
        parse_inference_response(ModelKind.CHURN, manifest, 1, response)
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("model_kind", [ModelKind.CHURN, ModelKind.FRAUD, ModelKind.QOE])
def test_standard_runtime_parameters_preserve_strict_response_validation(
    model_kind: ModelKind,
) -> None:
    output: dict[str, object]
    if model_kind == ModelKind.CHURN:
        manifest = _churn_manifest()
        output = {"name": "predict", "shape": [1], "datatype": "FP64", "data": [0.75]}
    elif model_kind == ModelKind.FRAUD:
        manifest = _fraud_manifest()
        output = {"name": "fraud_class", "shape": [1], "datatype": "INT64", "data": [1]}
    else:
        manifest = _qoe_manifest()
        output = {"name": "output", "shape": [1, 1], "datatype": "FP32", "data": [72.5]}
    output["parameters"] = {"content_type": "np"}
    response = _response(manifest, [output])
    response["parameters"] = {"headers": {"x-runtime": "mlserver"}}
    assert _contract_accepts(model_kind, response, response=True)
    assert (
        parse_inference_response(model_kind, manifest, 1, response).model_version
        == manifest.model_version
    )
    response["unexpected"] = "must still fail"
    with pytest.raises(ServiceError):
        parse_inference_response(model_kind, manifest, 1, response)


@pytest.mark.parametrize(("field", "value"), [("shape", [1, 2]), ("name", "wrong-output")])
def test_churn_probability_metadata_mismatch_is_rejected(field: str, value: object) -> None:
    manifest = _churn_manifest()
    output: dict[str, object] = {
        "name": "predict",
        "shape": [1],
        "datatype": "FP64",
        "data": [0.75],
    }
    output[field] = value
    response = _response(manifest, [output])
    with pytest.raises(ServiceError):
        parse_inference_response(ModelKind.CHURN, manifest, 1, response)


def test_fraud_accepts_numpy_codec_unit_dimension_with_flat_labels() -> None:
    manifest = _fraud_manifest()
    output = dict(_fraud_class_output(), shape=[2, 1], parameters={"content_type": "np"})
    response = _response(manifest, [output])
    assert _contract_accepts(ModelKind.FRAUD, response, response=True)
    assert parse_inference_response(ModelKind.FRAUD, manifest, 2, response).values == (0, 1)
