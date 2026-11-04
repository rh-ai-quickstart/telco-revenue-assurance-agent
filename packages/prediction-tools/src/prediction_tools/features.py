"""Validated model manifests and training-time feature transforms."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol, cast

import joblib  # type: ignore[import-untyped]  # Joblib does not ship inline type metadata.
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.errors import ErrorCode, ServiceError


class ModelKind(StrEnum):
    """Stable internal identifier for a KServe model."""

    CHURN = "churn"
    FRAUD = "fraud"
    QOE = "qoe"


MODEL_FILES: dict[ModelKind, str] = {
    ModelKind.CHURN: "churn.json",
    ModelKind.FRAUD: "fraud.json",
    ModelKind.QOE: "qoe.json",
}
MODEL_NAMES: dict[ModelKind, str] = {
    ModelKind.CHURN: "churn-prediction",
    ModelKind.FRAUD: "fraud-prediction",
    ModelKind.QOE: "starlink-qoe",
}
QOE_FEATURE_ORDER = (
    "latitude",
    "longitude",
    "elevation_m",
    "visible_satellites",
    "serving_satellites",
    "signal_loss_db",
    "season_encoded",
    "weather_encoded",
)
CHURN_CATEGORICAL_MAPPINGS: dict[str, dict[str, float]] = {
    "PaymentStatus": {"Paid": 0.0, "Partial": 0.5, "Unpaid": 1.0},
    "PrimaryIssueType": {
        "": 0.0,
        "Billing": 0.7,
        "Technical": 0.8,
        "Service Quality": 0.6,
    },
    "SupportChannel": {"": 0.0, "Phone": 1.0, "Chat": 0.5, "Email": 0.2},
}


class ModelFeatureManifest(BaseModel):
    """Common manifest data supplied alongside the external model artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model_name: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    input_tensor_name: str = Field(min_length=1)
    output_tensor_name: str = Field(min_length=1)
    feature_order: list[str] = Field(min_length=1)
    feature_width: int = Field(gt=0)


class FraudFeatureManifest(ModelFeatureManifest):
    """Fraud artifact metadata for one-hot features and fixed class ordering."""

    plan_type_categories: list[str] = Field(min_length=2)
    class_order: list[bool] = Field(min_length=2, max_length=2)


class QoeFeatureManifest(ModelFeatureManifest):
    """QoE artifact metadata and fixed names for its external preprocessing sidecars."""

    scaler_file: Literal["scaler.joblib"]
    label_encoders_file: Literal["label_encoders.joblib"]


type FeatureManifest = ModelFeatureManifest | FraudFeatureManifest | QoeFeatureManifest


class Transformer(Protocol):
    """Minimal interface implemented by joblib-loaded preprocessing artifacts."""

    def transform(self, values: object, /) -> object: ...


class QoePreprocessor:
    """Apply the supplied label encoders, then the trained eight-feature scaler."""

    def __init__(self, scaler: Transformer, encoders: Mapping[str, Transformer]) -> None:
        self._scaler = scaler
        self._encoders = encoders

    def transform(self, raw_features: Mapping[str, object]) -> tuple[float, ...]:
        numeric_names = QOE_FEATURE_ORDER[:6]
        values: dict[str, float] = {
            name: _finite_number(raw_features.get(name), name) for name in numeric_names
        }
        for feature_name in ("season", "weather"):
            label = raw_features.get(feature_name)
            if not isinstance(label, str):
                raise _feature_mismatch()
            encoder = self._encoders.get(feature_name)
            if encoder is None:
                raise _feature_mismatch()
            try:
                encoded = np.asarray(encoder.transform(np.asarray([label], dtype=object)))
            except Exception as exc:
                raise _feature_mismatch() from exc
            if encoded.shape != (1,):
                raise _feature_mismatch()
            values[f"{feature_name}_encoded"] = _finite_number(encoded[0], feature_name)

        unscaled = np.asarray([[values[name] for name in QOE_FEATURE_ORDER]], dtype=np.float64)
        try:
            scaled = np.asarray(self._scaler.transform(unscaled), dtype=np.float64)
        except Exception as exc:
            raise _feature_mismatch() from exc
        if scaled.shape != (1, len(QOE_FEATURE_ORDER)) or not np.isfinite(scaled).all():
            raise _feature_mismatch()
        return tuple(float(value) for value in scaled[0])


class FeatureRegistry:
    """Read fixed manifest names and QoE sidecars from the configured mount."""

    def __init__(self, manifest_directory: Path) -> None:
        self._directory = manifest_directory
        self._lock = RLock()
        self._manifests: dict[ModelKind, FeatureManifest] = {}
        self._qoe_preprocessor: QoePreprocessor | None = None

    def manifest_for(self, model_kind: ModelKind) -> FeatureManifest:
        if model_kind not in MODEL_FILES:
            raise ValueError("Unsupported model kind.")
        with self._lock:
            cached = self._manifests.get(model_kind)
            if cached is not None:
                return cached
            manifest = self._load_manifest(model_kind)
            self._validate_manifest(model_kind, manifest)
            self._manifests[model_kind] = manifest
            return manifest

    def qoe_preprocessor(self, manifest: QoeFeatureManifest) -> QoePreprocessor:
        with self._lock:
            if self._qoe_preprocessor is not None:
                return self._qoe_preprocessor
            try:
                scaler_path = self._sidecar_path(manifest.scaler_file)
                encoders_path = self._sidecar_path(manifest.label_encoders_file)
                scaler_loaded = joblib.load(scaler_path)
                encoders_loaded = joblib.load(encoders_path)
                if not isinstance(encoders_loaded, Mapping):
                    raise ValueError("invalid label encoder artifact")
                encoders_by_name: dict[str, Transformer] = {}
                for key, value in encoders_loaded.items():
                    if not isinstance(key, str) or not callable(getattr(value, "transform", None)):
                        raise ValueError("invalid label encoder entry")
                    normalized_key = key.casefold()
                    if normalized_key in encoders_by_name:
                        raise ValueError("duplicate label encoder entry")
                    encoders_by_name[normalized_key] = cast(Transformer, value)
                if not all(name in encoders_by_name for name in ("season", "weather")):
                    raise ValueError("required label encoders are missing")
                if not callable(getattr(scaler_loaded, "transform", None)):
                    raise ValueError("invalid scaler artifact")
                input_width = getattr(scaler_loaded, "n_features_in_", len(QOE_FEATURE_ORDER))
                if int(input_width) != len(QOE_FEATURE_ORDER):
                    raise ValueError("scaler feature width differs from the contract")
                self._qoe_preprocessor = QoePreprocessor(
                    cast(Transformer, scaler_loaded), encoders_by_name
                )
                return self._qoe_preprocessor
            except ServiceError:
                raise
            except Exception as exc:
                raise _feature_mismatch() from exc

    def _load_manifest(self, model_kind: ModelKind) -> FeatureManifest:
        manifest_path = self._directory / MODEL_FILES[model_kind]
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root must be an object")
            if model_kind == ModelKind.FRAUD:
                return FraudFeatureManifest.model_validate(raw)
            if model_kind == ModelKind.QOE:
                return QoeFeatureManifest.model_validate(raw)
            return ModelFeatureManifest.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise _feature_mismatch() from exc

    def _sidecar_path(self, name: str) -> Path:
        directory = self._directory.resolve(strict=True)
        candidate = (self._directory / name).resolve(strict=True)
        if candidate.parent != directory or not candidate.is_file():
            raise ValueError("preprocessing sidecar is outside the configured directory")
        return candidate

    @staticmethod
    def _validate_manifest(model_kind: ModelKind, manifest: FeatureManifest) -> None:
        if manifest.model_name != MODEL_NAMES[model_kind]:
            raise _feature_mismatch()
        if len(manifest.feature_order) != manifest.feature_width:
            raise _feature_mismatch()
        if len(set(manifest.feature_order)) != manifest.feature_width:
            raise _feature_mismatch()
        if model_kind == ModelKind.CHURN:
            for name in manifest.feature_order:
                normalized = "".join(char for char in name.casefold() if char.isalnum())
                if (
                    normalized in {"haschurned", "customerid", "billingcyclestart"}
                    or "date" in normalized
                    or "cycle" in normalized
                ):
                    raise _feature_mismatch()
        elif model_kind == ModelKind.FRAUD:
            if not isinstance(manifest, FraudFeatureManifest):
                raise _feature_mismatch()
            if manifest.class_order != [False, True]:
                raise _feature_mismatch()
            categories = manifest.plan_type_categories
            if len(set(categories)) != len(categories) or any(not value for value in categories):
                raise _feature_mismatch()
            dummy_names = {f"Plan_Type_{value}" for value in categories[1:]}
            present_dummy_names = {
                name for name in manifest.feature_order if name.startswith("Plan_Type_")
            }
            if (
                "Plan_Type" in manifest.feature_order
                or present_dummy_names != dummy_names
                or not dummy_names.issubset(manifest.feature_order)
            ):
                raise _feature_mismatch()
        elif model_kind == ModelKind.QOE and (
            not isinstance(manifest, QoeFeatureManifest)
            or tuple(manifest.feature_order) != QOE_FEATURE_ORDER
            or manifest.feature_width != len(QOE_FEATURE_ORDER)
        ):
            raise _feature_mismatch()


def transform_churn_features(
    manifest: ModelFeatureManifest, raw_features: Mapping[str, object]
) -> tuple[float, ...]:
    """Apply the source pipeline's categorical mapping in manifest order."""

    if any(_is_excluded_churn_feature(name) for name in manifest.feature_order):
        raise _feature_mismatch()
    transformed: list[float] = []
    for name in manifest.feature_order:
        raw_value = raw_features.get(name)
        categorical_mapping = CHURN_CATEGORICAL_MAPPINGS.get(name)
        if categorical_mapping is not None:
            if (
                name in {"PrimaryIssueType", "SupportChannel"}
                and name in raw_features
                and raw_value is None
            ):
                # The source pipeline fills nulls only for these two categories.
                raw_value = ""
            if not isinstance(raw_value, str) or raw_value not in categorical_mapping:
                raise _feature_mismatch()
            transformed.append(categorical_mapping[raw_value])
        elif name == "EscalationFlag" and isinstance(raw_value, (bool, np.bool_)):
            # The source dataframe trains this boolean flag as numeric 0/1.
            transformed.append(float(raw_value))
        else:
            transformed.append(_finite_number(raw_value, name))
    if len(transformed) != manifest.feature_width:
        raise _feature_mismatch()
    return tuple(transformed)


def transform_fraud_features(
    manifest: FraudFeatureManifest, raw_features: Mapping[str, object]
) -> tuple[float, ...]:
    """Apply training-time Plan_Type one-hot encoding with drop-first semantics."""

    plan_type = raw_features.get("Plan_Type")
    if not isinstance(plan_type, str) or plan_type not in manifest.plan_type_categories:
        raise _feature_mismatch()
    dummy_values = {
        f"Plan_Type_{category}": float(plan_type == category)
        for category in manifest.plan_type_categories[1:]
    }
    transformed: list[float] = []
    for name in manifest.feature_order:
        if name.startswith("Plan_Type_"):
            transformed.append(dummy_values[name])
        elif name == "Plan_Type":
            raise _feature_mismatch()
        else:
            transformed.append(_finite_number(raw_features.get(name), name))
    if len(transformed) != manifest.feature_width:
        raise _feature_mismatch()
    return tuple(transformed)


def transform_qoe_features(
    manifest: QoeFeatureManifest,
    raw_features: Mapping[str, object],
    preprocessor: QoePreprocessor,
) -> tuple[float, ...]:
    """Encode season/weather and scale all eight trained QoE inputs."""

    if tuple(manifest.feature_order) != QOE_FEATURE_ORDER:
        raise _feature_mismatch()
    return preprocessor.transform(raw_features)


def feature_snapshot(values: Sequence[float]) -> str:
    """Hash a canonical numeric model-input row for cache invalidation."""

    normalized = [float(value) for value in values]
    if not all(math.isfinite(value) for value in normalized):
        raise _feature_mismatch()
    encoded = json.dumps(normalized, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_excluded_churn_feature(name: str) -> bool:
    normalized = "".join(char for char in name.casefold() if char.isalnum())
    return (
        normalized == "haschurned"
        or normalized.endswith("id")
        or "identifier" in normalized
        or "date" in normalized
        or "cycle" in normalized
    )


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        raise _feature_mismatch()
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _feature_mismatch() from exc
    if not math.isfinite(number):
        raise _feature_mismatch()
    return number


def _feature_mismatch() -> ServiceError:
    return ServiceError(
        ErrorCode.FEATURE_SCHEMA_MISMATCH,
        "Model feature metadata or source features do not match the deployed model contract.",
        retryable=False,
    )
