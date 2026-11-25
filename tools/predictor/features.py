"""Training-compatible feature transforms, independent of the serving backend."""

import io
import math
import os
import struct
from collections.abc import Mapping
from contextlib import closing
from functools import lru_cache

PAYMENT_MAPPING = {"Paid": 0, "Partial": 0.5, "Unpaid": 1}
ISSUE_MAPPING = {"": 0, "Billing": 0.7, "Technical": 0.8, "Service Quality": 0.6}
CHANNEL_MAPPING = {"": 0, "Phone": 1, "Chat": 0.5, "Email": 0.2}

# Explicit whitelists also exclude IDs, dates, targets, and unrelated fields.
CHURN_FEATURES = (
    "CallMinutes", "DataUsageGB", "SMSCount", "InternetSessions",
    "TotalBillAmount", "PaymentStatus", "LateFee", "DiscountsApplied",
    "SupportInteractionCount", "PrimaryIssueType", "ResolutionTimeMinutes",
    "SatisfactionScore", "EscalationFlag", "SupportChannel",
)
CHURN_DB_FIELDS = (
    "call_minutes", "data_usage_gb", "sms_count", "internet_sessions",
    "total_bill_amount", "payment_status", "late_fee", "discounts_applied",
    "support_interaction_count", "primary_issue_type", "resolution_time_minutes",
    "satisfaction_score", "escalation_flag", "support_channel",
)
FRAUD_FEATURES = (
    "Call_Duration", "Data_Usage", "Sms_Count", "Roaming_Indicator",
    "MobileWallet_Use", "Cost", "Cellular_Location_Distance", "Personal_Pin_Used",
    "Avg_Call_Duration", "Avg_Data_Usage", "Avg_Cost", "Plan_Type_prepaid",
)
FRAUD_DB_FIELDS = (
    "call_duration", "data_usage", "sms_count", "roaming_indicator",
    "mobile_wallet_use", "cost", "cellular_location_distance", "personal_pin_used",
    "avg_call_duration", "avg_data_usage", "avg_cost",
)
QOE_FEATURES = (
    "latitude", "longitude", "elevation_m", "visible_satellites",
    "serving_satellites", "signal_loss_db", "season_encoded", "weather_encoded",
)


class PreprocessingError(ValueError):
    """Required source fields or fitted preprocessing artifacts are invalid."""


def _field(record: Mapping, name: str, alias: str | None = None):
    if not isinstance(record, Mapping):
        raise PreprocessingError("Customer data must be a field mapping")
    for key in (name, alias):
        if key is not None and key in record:
            return record[key]
    raise PreprocessingError(f"Missing required feature: {name}")


def _fp32(value, name: str) -> float:
    try:
        result = struct.unpack("f", struct.pack("f", float(value)))[0]
    except (TypeError, ValueError, OverflowError, struct.error):
        raise PreprocessingError(f"Feature {name} must be a finite FP32 number") from None
    if not math.isfinite(result):
        raise PreprocessingError(f"Feature {name} must be a finite FP32 number")
    return result


def _category(value, mapping: dict, name: str, *, empty_if_missing=False):
    if empty_if_missing and (value is None or (isinstance(value, float) and math.isnan(value))):
        value = ""
    try:
        return mapping[value]
    except (KeyError, TypeError):
        raise PreprocessingError(f"Unsupported category for {name}") from None


def preprocess_churn(record: Mapping) -> list[float]:
    """Select the 14 training features and encode billing/support categories."""
    values = {name: _field(record, name, alias)
              for name, alias in zip(CHURN_FEATURES, CHURN_DB_FIELDS)}
    values["PaymentStatus"] = _category(values["PaymentStatus"], PAYMENT_MAPPING, "PaymentStatus")
    values["PrimaryIssueType"] = _category(
        values["PrimaryIssueType"], ISSUE_MAPPING, "PrimaryIssueType", empty_if_missing=True,
    )
    values["SupportChannel"] = _category(
        values["SupportChannel"], CHANNEL_MAPPING, "SupportChannel", empty_if_missing=True,
    )
    flag = values["EscalationFlag"]
    if not isinstance(flag, (bool, int, float)) or flag not in (0, 1):
        raise PreprocessingError("EscalationFlag must be a boolean or 0/1")
    values["EscalationFlag"] = int(flag)
    return [_fp32(values[name], name) for name in CHURN_FEATURES]


def preprocess_fraud(record: Mapping) -> list[float]:
    """Match pandas.get_dummies ordering: the prepaid indicator is last."""
    values = [_fp32(_field(record, name, alias), name)
              for name, alias in zip(FRAUD_FEATURES[:-1], FRAUD_DB_FIELDS)]
    plan_type = _field(record, "Plan_Type", "plan_type")
    if not isinstance(plan_type, str) or not plan_type.strip():
        raise PreprocessingError("Plan_Type must be a nonempty string")
    values.append(float(plan_type.strip().lower() == "prepaid"))
    return values


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PreprocessingError(f"Set {name} to load the fitted QoE artifacts")
    return value


@lru_cache(maxsize=4)
def _load_artifacts(endpoint: str, bucket: str, prefix: str):
    """Load and reuse fitted QoE preprocessing artifacts from S3."""
    # Load only trusted training artifacts. Joblib is a pickle-based format.
    import boto3
    import joblib

    s3 = boto3.client("s3", endpoint_url=endpoint)

    def load(name: str):
        response = s3.get_object(
            Bucket=bucket, Key=f"{prefix}/artifacts/{name}.joblib"
        )
        with closing(response["Body"]) as body:
            return joblib.load(io.BytesIO(body.read()))

    return load("scaler"), load("label_encoders")


def _load_qoe_artifacts():
    """Resolve the S3 location of the deployed QoE model's scaler/encoders."""
    return _load_artifacts(
        _required_env("AWS_S3_ENDPOINT"),
        _required_env("S3_BUCKET"),
        _required_env("S3_MODEL_PATH").strip("/"),
    )


def clear_preprocessing_artifacts() -> None:
    """Reload fitted artifacts on the next prediction after a model update."""
    _load_artifacts.cache_clear()


def preprocess_qoe(record: Mapping) -> list[float]:
    """Encode season/weather, then apply the fitted eight-feature scaler."""
    # Validate fields before downloading artifacts or sending any request.
    numeric = [_field(record, name) for name in QOE_FEATURES[:6]]
    for name, value in zip(QOE_FEATURES, numeric):
        _fp32(value, name)
    season, weather = _field(record, "season"), _field(record, "weather")
    scaler, encoders = _load_qoe_artifacts()
    try:
        encoded = [int(encoders[name].transform([value])[0])
                   for name, value in (("season", season), ("weather", weather))]
    except (ValueError, KeyError, TypeError):
        raise PreprocessingError("QoE season/weather must match the fitted label encoders") from None

    import numpy as np

    raw = np.asarray([[float(value) for value in numeric] + encoded], dtype=float)
    # The pipeline fits StandardScaler on a DataFrame with these names.
    if hasattr(scaler, "feature_names_in_"):
        import pandas as pd

        if list(scaler.feature_names_in_) != list(QOE_FEATURES):
            raise PreprocessingError("QoE scaler feature order does not match the model")
        raw = pd.DataFrame(raw, columns=QOE_FEATURES)
    scaled = np.asarray(scaler.transform(raw))
    if scaled.shape != (1, 8):
        raise PreprocessingError("QoE scaler must produce exactly eight features")
    return [_fp32(value, name) for name, value in zip(QOE_FEATURES, scaled[0])]
