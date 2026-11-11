"""HTTP API for customer-ID prediction tools and model batching."""

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from starlette.concurrency import run_in_threadpool

from . import predictors
from .client import DataFetchError
from .features import PreprocessingError
from .predictors import InferenceError

log = logging.getLogger(__name__)
ModelName = Literal["churn", "fraud", "qoe"]


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: StrictStr

    @field_validator("customer_id")
    @classmethod
    def nonblank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("customer_id must be a nonblank string")
        return value


class BatchPredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_ids: Annotated[list[StrictStr], Field(min_length=1, max_length=1000)]

    @field_validator("customer_ids")
    @classmethod
    def nonblank_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("customer_ids must contain nonblank strings")
        return values


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await run_in_threadpool(predictors.shutdown_prediction_workers)


app = FastAPI(
    title="Prediction Tools",
    description="Customer-ID predictions with preprocessing, caching, and model batching",
    version="1.0.0",
    lifespan=lifespan,
)


def _predict(model: ModelName, customer_ids: list[str]) -> list[dict]:
    try:
        return predictors.predict_batch(model, customer_ids)
    except PreprocessingError as error:
        # These exceptions contain validation messages with static field names;
        # unlike SDK failures, they do not include raw values or credentials.
        raise HTTPException(status_code=422, detail=str(error)) from None
    except LookupError:
        raise HTTPException(
            status_code=404, detail="Required customer data was not found"
        ) from None
    except (DataFetchError, InferenceError):
        raise HTTPException(
            status_code=502, detail="An upstream prediction dependency failed"
        ) from None
    except Exception:  # noqa: BLE001 - Sanitize SDK failures at the HTTP boundary.
        # Exceptions from SDKs can contain URLs, credentials, or source records.
        # Log a fixed message and never return the original exception text.
        log.error("Prediction configuration or infrastructure failed")
        raise HTTPException(
            status_code=500, detail="Prediction service could not complete the request"
        ) from None


@app.get("/health")
def health() -> dict:
    """Process liveness only; does not call the data service or model endpoints."""
    return {"status": "ok"}


@app.get("/stats")
def stats() -> dict:
    """Aggregate process-local cache and inference statistics without customer data."""
    try:
        return predictors.prediction_stats()
    except Exception:  # noqa: BLE001 - Keep infrastructure details out of HTTP responses.
        log.error("Prediction statistics could not be collected")
        raise HTTPException(
            status_code=500, detail="Prediction statistics are unavailable"
        ) from None


@app.post("/predict/{model}")
def predict(model: ModelName, request: PredictionRequest) -> dict:
    """Return a domain prediction for one customer."""
    return _predict(model, [request.customer_id])[0]


@app.post("/predict/{model}/batch")
def predict_batch(model: ModelName, request: BatchPredictionRequest) -> dict:
    """Return predictions in input order, including repeated customer IDs."""
    results = _predict(model, request.customer_ids)
    predictions = [
        {"customer_id": customer_id, **result}
        for customer_id, result in zip(request.customer_ids, results, strict=True)
    ]
    return {"model": model, "predictions": predictions, "count": len(predictions)}
