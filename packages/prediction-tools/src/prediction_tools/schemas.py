"""Typed schemas for the frozen prediction MCP contract."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt

from common.models import CommonModel

CustomerId = Annotated[str, Field(min_length=1, max_length=200)]
TransactionId = Annotated[str, Field(min_length=1, max_length=200)]
IdentifierBatch = Annotated[list[CustomerId], Field(min_length=1, max_length=100)]
ModelVersion = Annotated[str, Field(min_length=1)]


class PredictChurnInput(CommonModel):
    """Bounded customer batch for latest-cycle churn prediction."""

    customer_ids: IdentifierBatch


class PredictFraudInput(CommonModel):
    """Bounded transaction batch for fraud classification."""

    transaction_ids: Annotated[list[TransactionId], Field(min_length=1, max_length=100)]


class PredictQoeInput(CommonModel):
    """Bounded customer batch for assigned-sample QoE prediction."""

    customer_ids: IdentifierBatch


class ChurnPrediction(CommonModel):
    """Raw class output from the LightGBM churn artifact."""

    customer_id: CustomerId
    class_id: StrictInt
    model_version: ModelVersion


class FraudPrediction(CommonModel):
    """Fraud class normalized using the fixed ONNX [False, True] class order."""

    transaction_id: TransactionId
    class_id: Literal[0, 1]
    label: Literal["Non-Fraud", "Fraud"]
    is_fraud: bool
    model_version: ModelVersion


class QoePrediction(CommonModel):
    """QoE score on the model's 0 to 100 scale."""

    customer_id: CustomerId
    score: Annotated[float, Field(ge=0, le=100)]
    model_version: ModelVersion


class PredictChurnOutput(CommonModel):
    """Ordered churn predictions for unique input customers."""

    predictions: list[ChurnPrediction]


class PredictFraudOutput(CommonModel):
    """Ordered fraud predictions for unique input transactions."""

    predictions: list[FraudPrediction]


class PredictQoeOutput(CommonModel):
    """Ordered QoE predictions for unique input customers."""

    predictions: list[QoePrediction]
