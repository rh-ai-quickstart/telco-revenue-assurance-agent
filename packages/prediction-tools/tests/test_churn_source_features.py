"""Preserve the source pipeline's boolean and nullable-category semantics."""

from __future__ import annotations

import numpy as np
import pytest
from prediction_tools.features import ModelFeatureManifest, transform_churn_features

from common.errors import ErrorCode, ServiceError


def _manifest(name: str) -> ModelFeatureManifest:
    return ModelFeatureManifest(
        model_name="churn-prediction",
        model_version="churn-feature-test",
        input_tensor_name="predict",
        output_tensor_name="predict",
        feature_order=[name],
        feature_width=1,
    )


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(False, 0.0), (True, 1.0), (np.bool_(False), 0.0), (np.bool_(True), 1.0)],
)
def test_escalation_flag_boolean_matches_trained_numeric_tensor(
    flag: object, expected: float
) -> None:
    result = transform_churn_features(_manifest("EscalationFlag"), {"EscalationFlag": flag})
    assert result == (expected,)


@pytest.mark.parametrize("flag", ["False", "True", None])
def test_escalation_flag_does_not_guess_string_or_missing_values(flag: object) -> None:
    with pytest.raises(ServiceError) as error:
        transform_churn_features(_manifest("EscalationFlag"), {"EscalationFlag": flag})
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("flag", [False, True, np.bool_(False), np.bool_(True)])
def test_boolean_remains_invalid_for_other_numeric_fields(flag: object) -> None:
    with pytest.raises(ServiceError) as error:
        transform_churn_features(_manifest("CallMinutes"), {"CallMinutes": flag})
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("name", ["PrimaryIssueType", "SupportChannel"])
def test_explicit_null_category_matches_source_empty_category(name: str) -> None:
    assert transform_churn_features(_manifest(name), {name: None}) == (0.0,)
    assert transform_churn_features(_manifest(name), {name: ""}) == (0.0,)


@pytest.mark.parametrize("name", ["PrimaryIssueType", "SupportChannel", "PaymentStatus"])
def test_missing_category_is_rejected(name: str) -> None:
    with pytest.raises(ServiceError) as error:
        transform_churn_features(_manifest(name), {})
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


def test_payment_status_null_is_rejected() -> None:
    with pytest.raises(ServiceError) as error:
        transform_churn_features(_manifest("PaymentStatus"), {"PaymentStatus": None})
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH


@pytest.mark.parametrize("name", ["PrimaryIssueType", "SupportChannel"])
@pytest.mark.parametrize("value", [float("nan"), "nan", False, 0.0, "unrecognized"])
def test_nullable_category_rejects_other_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ServiceError) as error:
        transform_churn_features(_manifest(name), {name: value})
    assert error.value.code == ErrorCode.FEATURE_SCHEMA_MISMATCH
