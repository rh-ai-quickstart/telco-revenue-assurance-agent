"""Prediction MCP tool, cache, ordering, and error contract tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest
from data_fetch_tool import (
    DataFetchClient,
    FetchCustomerDataInput,
    FetchCustomerDataOutput,
)
from fastmcp import Client
from prediction_tools.features import (
    QOE_FEATURE_ORDER,
    FeatureRegistry,
    FraudFeatureManifest,
    ModelFeatureManifest,
    ModelKind,
    QoeFeatureManifest,
)
from prediction_tools.kserve_client import InferenceBatch, KServeClientProtocol
from prediction_tools.main import PredictionService, create_mcp_server
from prediction_tools.schemas import PredictChurnInput

from common.errors import ErrorCode, ServiceError, UpstreamError

_EXPECTED_TOOLS = {"predict_churn", "predict_fraud", "predict_qoe"}
_QOE_FEATURES = {
    "latitude": 1.0,
    "longitude": 2.0,
    "elevation_m": 3.0,
    "visible_satellites": 4.0,
    "serving_satellites": 5.0,
    "signal_loss_db": 6.0,
    "season": "winter",
    "weather": "clear",
}


class FeatureDataFetchClient:
    """Deterministic protocol fake returning only the requested feature alias."""

    def __init__(
        self,
        rows: dict[str, dict[str, dict[str, object]]],
    ) -> None:
        self.rows = rows
        self.requests: list[FetchCustomerDataInput] = []
        self.timeout_next = False

    async def fetch_customer_data(self, request: FetchCustomerDataInput) -> FetchCustomerDataOutput:
        self.requests.append(request)
        if self.timeout_next:
            self.timeout_next = False
            raise TimeoutError("test timeout")
        if request.ids is None or not request.field_aliases:
            raise AssertionError("the consumer must request bounded IDs and one feature alias")
        alias = str(request.field_aliases[0])
        source_rows = self.rows.get(str(request.entity), {})
        records: list[dict[str, object]] = []
        for subject_id in reversed(request.ids):
            subject = source_rows.get(subject_id)
            if subject is not None:
                records.append(
                    {
                        "entity": str(request.entity),
                        "record_id": subject_id,
                        "attributes": {alias: subject[alias]},
                    }
                )
        return FetchCustomerDataOutput.model_validate({"records": records, "next_cursor": None})


class FakeKServeClient:
    """In-process model fake that records exact rows and can fail one request."""

    def __init__(self) -> None:
        self.calls: list[tuple[ModelKind, tuple[tuple[float, ...], ...]]] = []
        self.failures_remaining = 0

    async def infer(
        self,
        model_kind: ModelKind,
        manifest: ModelFeatureManifest | FraudFeatureManifest | QoeFeatureManifest,
        feature_rows: Sequence[Sequence[float]],
    ) -> InferenceBatch:
        rows = tuple(tuple(row) for row in feature_rows)
        self.calls.append((model_kind, rows))
        await asyncio.sleep(0.01)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise UpstreamError(
                ErrorCode.MODEL_UNAVAILABLE,
                "The requested model is temporarily unavailable.",
                retryable=True,
            )
        if model_kind == ModelKind.FRAUD:
            values: tuple[int | float, ...] = tuple(1 if row[0] >= 10.0 else 0 for row in rows)
        elif model_kind == ModelKind.QOE:
            values = tuple(78.4 + index for index, _ in enumerate(rows))
        else:
            values = tuple(int(row[-1] % 2) for row in rows)
        return InferenceBatch(model_version=manifest.model_version, values=values)


@pytest.fixture
def service_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient]:
    manifest_directory = tmp_path / "manifests"
    manifest_directory.mkdir()
    manifests: dict[str, dict[str, object]] = {
        "churn.json": {
            "model_name": "churn-prediction",
            "model_version": "churn-release-v1",
            "input_tensor_name": "churn_input",
            "output_tensor_name": "churn_class",
            "feature_order": [
                "PaymentStatus",
                "PrimaryIssueType",
                "SupportChannel",
                "Balance",
            ],
            "feature_width": 4,
        },
        "fraud.json": {
            "model_name": "fraud-prediction",
            "model_version": "fraud-release-v1",
            "input_tensor_name": "fraud_input",
            "output_tensor_name": "fraud_class",
            "feature_order": ["amount", "Plan_Type_Standard", "Plan_Type_Premium"],
            "feature_width": 3,
            "plan_type_categories": ["Basic", "Standard", "Premium"],
            "class_order": [False, True],
        },
        "qoe.json": {
            "model_name": "starlink-qoe",
            "model_version": "qoe-release-v1",
            "input_tensor_name": "qoe_input",
            "output_tensor_name": "qoe_score",
            "feature_order": list(QOE_FEATURE_ORDER),
            "feature_width": 8,
            "scaler_file": "scaler.joblib",
            "label_encoders_file": "label_encoders.joblib",
        },
    }
    for filename, manifest in manifests.items():
        (manifest_directory / filename).write_text(json.dumps(manifest), encoding="utf-8")
    (manifest_directory / "scaler.joblib").write_bytes(b"test-sidecar")
    (manifest_directory / "label_encoders.joblib").write_bytes(b"test-sidecar")

    class FakeLabelEncoder:
        def transform(self, values: object, /) -> object:
            labels = np.asarray(values, dtype=object)
            return np.asarray([1 if str(value) == "winter" else 0 for value in labels])

    class FakeScaler:
        n_features_in_ = 8

        def transform(self, values: object, /) -> object:
            return np.asarray(values, dtype=np.float64) / 10.0

    def load_sidecar(path: str | Path) -> object:
        if Path(path).name == "scaler.joblib":
            return FakeScaler()
        return {"season": FakeLabelEncoder(), "weather": FakeLabelEncoder()}

    from prediction_tools import features as feature_module

    monkeypatch.setattr(feature_module.__dict__["joblib"], "load", load_sidecar)

    source_rows: dict[str, dict[str, dict[str, object]]] = {
        "customers": {
            "customer-1": {
                "churn_features": {
                    "PaymentStatus": "Paid",
                    "PrimaryIssueType": "Billing",
                    "SupportChannel": "Phone",
                    "Balance": 21.0,
                    "HasChurned": 1,
                    "BillingCycleStart": "2026-01-01",
                    "CustomerID": "customer-1",
                },
                "qoe_features": dict(_QOE_FEATURES),
            },
            "customer-2": {
                "churn_features": {
                    "PaymentStatus": "Unpaid",
                    "PrimaryIssueType": "Technical",
                    "SupportChannel": "Chat",
                    "Balance": 42.0,
                    "HasChurned": 0,
                    "BillingCycleStart": "2026-02-01",
                    "CustomerID": "customer-2",
                },
                "qoe_features": dict(_QOE_FEATURES),
            },
        },
        "transactions": {
            "transaction-1": {"fraud_features": {"amount": 5.0, "Plan_Type": "Basic"}},
            "transaction-2": {"fraud_features": {"amount": 25.0, "Plan_Type": "Premium"}},
        },
    }
    data_fetch_client = FeatureDataFetchClient(source_rows)
    kserve_client = FakeKServeClient()
    protocol_client: DataFetchClient = data_fetch_client
    protocol_kserve: KServeClientProtocol = kserve_client
    service = PredictionService(
        data_fetch_client=protocol_client,
        features=FeatureRegistry(manifest_directory),
        kserve_client=protocol_kserve,
    )
    return service, data_fetch_client, kserve_client


def _resolve_schema_ref(schema: dict[str, object], reference: str) -> dict[str, object]:
    if not reference.startswith("#/"):
        raise AssertionError(f"unexpected non-local schema reference: {reference}")
    value: object = schema
    for token in reference[2:].split("/"):
        if not isinstance(value, dict):
            raise AssertionError("schema reference does not resolve to an object")
        value = value[token.replace("~1", "/").replace("~0", "~")]
    if not isinstance(value, dict):
        raise AssertionError("schema reference does not resolve to an object")
    return value


def _resolved_result_schema(tool_schema: dict[str, object]) -> dict[str, object]:
    current: object = tool_schema
    while isinstance(current, dict) and "$ref" in current:
        current = _resolve_schema_ref(tool_schema, str(current["$ref"]))
    if not isinstance(current, dict):
        raise AssertionError("tool output does not resolve to a JSON object schema")
    properties = current.get("properties")
    if not isinstance(properties, dict):
        raise AssertionError("tool output schema has no properties")
    predictions = properties.get("predictions")
    if not isinstance(predictions, dict):
        raise AssertionError("tool output schema has no predictions list")
    items = predictions.get("items")
    if not isinstance(items, dict):
        raise AssertionError("tool output predictions have no item schema")
    while "$ref" in items:
        items = _resolve_schema_ref(tool_schema, str(items["$ref"]))
    return items


@pytest.mark.asyncio
async def test_exact_tools_have_typed_schemas_and_resolve_after_reconnect(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, _, _ = service_bundle
    server = create_mcp_server(service)
    contract = json.loads(
        (Path(__file__).parents[3] / "contracts" / "prediction-mcp.json").read_text(
            encoding="utf-8"
        )
    )
    result_schemas = {
        "predict_churn": ("PredictChurnResult", {"customer_id", "class_id", "model_version"}),
        "predict_fraud": (
            "PredictFraudResult",
            {"transaction_id", "class_id", "label", "is_fraud", "model_version"},
        ),
        "predict_qoe": ("PredictQoeResult", {"customer_id", "score", "model_version"}),
    }

    for _ in range(2):
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == _EXPECTED_TOOLS
            for tool in tools:
                assert tool.output_schema is not None
                result_schema = _resolved_result_schema(tool.output_schema)
                contract_name, expected_fields = result_schemas[tool.name]
                expected_schema = contract["$defs"][contract_name]
                actual_properties = result_schema.get("properties")
                actual_required = result_schema.get("required")
                expected_properties = expected_schema.get("properties")
                expected_required = expected_schema.get("required")
                assert isinstance(actual_properties, dict)
                assert isinstance(actual_required, list)
                assert isinstance(expected_properties, dict)
                assert isinstance(expected_required, list)
                assert set(actual_properties) == expected_fields
                assert set(actual_required) == set(expected_required)
                for field_name in expected_fields:
                    actual_field = actual_properties[field_name]
                    expected_field = expected_properties[field_name]
                    assert isinstance(actual_field, dict)
                    assert isinstance(expected_field, dict)
                    if "$ref" in expected_field:
                        reference_name = expected_field["$ref"].rsplit("/", 1)[-1]
                        expected_field = contract["$defs"][reference_name]
                        assert isinstance(expected_field, dict)
                    for key in ("type", "enum", "minLength", "maxLength", "minimum", "maximum"):
                        if key in expected_field:
                            assert actual_field.get(key) == expected_field[key]


@pytest.mark.asyncio
async def test_mcp_outputs_are_typed_ordered_deduplicated_and_class_mapped(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, source, _ = service_bundle
    async with Client(create_mcp_server(service)) as client:
        churn = await client.call_tool(
            "predict_churn",
            {"customer_ids": ["customer-2", "customer-1", "customer-2"]},
            raise_on_error=False,
        )
        fraud = await client.call_tool(
            "predict_fraud",
            {"transaction_ids": ["transaction-1", "transaction-2"]},
            raise_on_error=False,
        )
        qoe = await client.call_tool(
            "predict_qoe",
            {"customer_ids": ["customer-1"]},
            raise_on_error=False,
        )

    assert not churn.is_error
    assert not fraud.is_error
    assert not qoe.is_error
    assert churn.structured_content is not None
    assert fraud.structured_content is not None
    assert qoe.structured_content is not None
    churn_predictions = churn.structured_content["predictions"]
    assert [item["customer_id"] for item in churn_predictions] == ["customer-2", "customer-1"]
    assert [item["model_version"] for item in churn_predictions] == [
        "churn-release-v1",
        "churn-release-v1",
    ]
    fraud_predictions = fraud.structured_content["predictions"]
    assert [item["class_id"] for item in fraud_predictions] == [0, 1]
    assert [item["label"] for item in fraud_predictions] == ["Non-Fraud", "Fraud"]
    assert [item["is_fraud"] for item in fraud_predictions] == [False, True]
    assert qoe.structured_content["predictions"][0]["score"] == 78.4
    assert [str(request.entity) for request in source.requests] == [
        "customers",
        "transactions",
        "customers",
    ]
    assert all(request.field_aliases for request in source.requests)
    assert [
        str(request.field_aliases[0]) for request in source.requests if request.field_aliases
    ] == [
        "churn_features",
        "fraud_features",
        "qoe_features",
    ]


@pytest.mark.asyncio
async def test_identical_concurrent_batches_share_one_inference(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, _, kserve = service_bundle
    request = PredictChurnInput(customer_ids=["customer-1", "customer-2"])

    first, second = await asyncio.gather(
        service.predict_churn(request),
        service.predict_churn(request),
    )

    assert first == second
    assert len(kserve.calls) == 1


@pytest.mark.asyncio
async def test_feature_snapshot_change_invalidates_success_cache(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, source, kserve = service_bundle
    request = PredictChurnInput(customer_ids=["customer-1"])

    await service.predict_churn(request)
    await service.predict_churn(request)
    assert len(kserve.calls) == 1

    changed_features = source.rows["customers"]["customer-1"]["churn_features"]
    assert isinstance(changed_features, dict)
    changed_features["Balance"] = 99.0
    await service.predict_churn(request)

    assert len(kserve.calls) == 2


@pytest.mark.asyncio
async def test_errors_are_structured_and_failed_inference_is_not_cached(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, source, kserve = service_bundle
    kserve.failures_remaining = 1
    async with Client(create_mcp_server(service)) as client:
        invalid = await client.call_tool(
            "predict_churn", {"customer_ids": []}, raise_on_error=False
        )
        missing = await client.call_tool(
            "predict_churn", {"customer_ids": ["not-found"]}, raise_on_error=False
        )
        failed = await client.call_tool(
            "predict_churn", {"customer_ids": ["customer-1"]}, raise_on_error=False
        )
        recovered = await client.call_tool(
            "predict_churn", {"customer_ids": ["customer-1"]}, raise_on_error=False
        )

    assert invalid.is_error
    assert invalid.structured_content["code"] == "INVALID_INPUT"
    assert missing.is_error
    assert missing.structured_content["code"] == "SUBJECT_NOT_FOUND"
    assert failed.is_error
    assert failed.structured_content["code"] == "MODEL_UNAVAILABLE"
    assert not recovered.is_error
    assert len(kserve.calls) == 2
    assert source.requests[-1].ids == ["customer-1"]


@pytest.mark.asyncio
async def test_source_deadline_maps_to_typed_timeout(
    service_bundle: tuple[PredictionService, FeatureDataFetchClient, FakeKServeClient],
) -> None:
    service, source, _ = service_bundle
    source.timeout_next = True

    with pytest.raises(ServiceError) as error:
        await service.predict_churn(PredictChurnInput(customer_ids=["customer-1"]))

    assert error.value.code == ErrorCode.UPSTREAM_TIMEOUT
