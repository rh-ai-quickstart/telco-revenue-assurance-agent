"""Agent-facing prediction tools accepting only customer IDs."""

from .client import DataFetchError
from .features import PreprocessingError, clear_preprocessing_artifacts
from .predictors import (
    InferenceError,
    clear_prediction_caches,
    predict_batch,
    predict_churn,
    predict_fraud,
    predict_qoe,
    prediction_stats,
    shutdown_prediction_workers,
)

# Pass these callables to the OGX agent's tool registration mechanism.
PREDICTION_TOOLS = (predict_churn, predict_fraud, predict_qoe)

__all__ = [
    "PREDICTION_TOOLS",
    "DataFetchError",
    "InferenceError",
    "PreprocessingError",
    "clear_prediction_caches",
    "clear_preprocessing_artifacts",
    "predict_batch",
    "predict_churn",
    "predict_fraud",
    "predict_qoe",
    "prediction_stats",
    "shutdown_prediction_workers",
]
