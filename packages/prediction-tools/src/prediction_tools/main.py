"""FastMCP prediction service for churn, fraud, and QoE inference."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from data_fetch_tool import DataFetchClient, FetchCustomerDataInput, FetchCustomerDataOutput
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool, ToolResult
from mcp.types import TextContent
from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, ValidationError, field_validator

from common.errors import ErrorCode, RequestError, ServiceError, UpstreamError
from common.logging import bind_request_id, configure_logging, get_request_id
from common.settings import BaseSettings

from .cache import BoundedSuccessCache, CacheKey
from .features import (
    FeatureRegistry,
    FraudFeatureManifest,
    ModelKind,
    QoeFeatureManifest,
    feature_snapshot,
    transform_churn_features,
    transform_fraud_features,
    transform_qoe_features,
)
from .kserve_client import (
    KServeClientProtocol,
    KServeEndpoint,
    KServeV2Client,
)
from .schemas import (
    ChurnPrediction,
    FraudPrediction,
    PredictChurnInput,
    PredictChurnOutput,
    PredictFraudInput,
    PredictFraudOutput,
    PredictQoeInput,
    PredictQoeOutput,
    QoePrediction,
)

logger = logging.getLogger(__name__)
_FETCH_DEADLINE_SECONDS = 30.0


class PredictionSettings(BaseSettings):
    """Environment-backed connection, manifest, and per-model Secret settings."""

    data_fetch_mcp_url: AnyHttpUrl
    churn_model_url: AnyHttpUrl
    fraud_model_url: AnyHttpUrl
    qoe_model_url: AnyHttpUrl
    model_feature_manifest_dir: Path
    churn_model_auth_token: SecretStr = Field(min_length=1)
    fraud_model_auth_token: SecretStr = Field(min_length=1)
    qoe_model_auth_token: SecretStr = Field(min_length=1)

    @field_validator("data_fetch_mcp_url", "churn_model_url", "fraud_model_url", "qoe_model_url")
    @classmethod
    def reject_embedded_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("service URLs must not contain credentials")
        return value


class PredictionService:
    """Coordinate MCP feature reads, preprocessing, KServe inference, and cache."""

    def __init__(
        self,
        *,
        data_fetch_client: DataFetchClient,
        features: FeatureRegistry,
        kserve_client: KServeClientProtocol,
        cache: BoundedSuccessCache | None = None,
    ) -> None:
        self._data_fetch = data_fetch_client
        self._features = features
        self._kserve = kserve_client
        self._cache = cache or BoundedSuccessCache()

    async def predict_churn(self, request: PredictChurnInput) -> PredictChurnOutput:
        customer_ids = _deduplicate(request.customer_ids)
        manifest = self._features.manifest_for(ModelKind.CHURN)
        feature_maps = await self._fetch_features(
            entity="customers",
            subject_ids=customer_ids,
            alias="churn_features",
        )
        transformed = {
            customer_id: transform_churn_features(manifest, feature_maps[customer_id])
            for customer_id in customer_ids
        }
        cache_keys = [
            CacheKey(
                tool_name="predict_churn",
                subject_id=customer_id,
                model_version=manifest.model_version,
                feature_snapshot=feature_snapshot(transformed[customer_id]),
            )
            for customer_id in customer_ids
        ]

        async def compute(keys: tuple[CacheKey, ...]) -> Sequence[object]:
            rows = [transformed[key.subject_id] for key in keys]
            inference = await self._kserve.infer(ModelKind.CHURN, manifest, rows)
            return [
                ChurnPrediction(
                    customer_id=key.subject_id,
                    class_id=int(value),
                    model_version=inference.model_version,
                )
                for key, value in zip(keys, inference.values, strict=True)
            ]

        cached = await self._cache.get_or_compute(cache_keys, compute)
        return PredictChurnOutput(predictions=cast(list[ChurnPrediction], cached))

    async def predict_fraud(self, request: PredictFraudInput) -> PredictFraudOutput:
        transaction_ids = _deduplicate(request.transaction_ids)
        manifest_value = self._features.manifest_for(ModelKind.FRAUD)
        if not isinstance(manifest_value, FraudFeatureManifest):
            raise _feature_error()
        manifest = manifest_value
        feature_maps = await self._fetch_features(
            entity="transactions",
            subject_ids=transaction_ids,
            alias="fraud_features",
        )
        transformed = {
            transaction_id: transform_fraud_features(manifest, feature_maps[transaction_id])
            for transaction_id in transaction_ids
        }
        cache_keys = [
            CacheKey(
                tool_name="predict_fraud",
                subject_id=transaction_id,
                model_version=manifest.model_version,
                feature_snapshot=feature_snapshot(transformed[transaction_id]),
            )
            for transaction_id in transaction_ids
        ]

        async def compute(keys: tuple[CacheKey, ...]) -> Sequence[object]:
            rows = [transformed[key.subject_id] for key in keys]
            inference = await self._kserve.infer(ModelKind.FRAUD, manifest, rows)
            predictions: list[FraudPrediction] = []
            for key, value in zip(keys, inference.values, strict=True):
                if type(value) is not int:
                    raise _feature_error()
                if value == 1:
                    class_id: Literal[0, 1] = 1
                elif value == 0:
                    class_id = 0
                else:
                    raise _feature_error()
                is_fraud = class_id == 1
                predictions.append(
                    FraudPrediction(
                        transaction_id=key.subject_id,
                        class_id=class_id,
                        label="Fraud" if is_fraud else "Non-Fraud",
                        is_fraud=is_fraud,
                        model_version=inference.model_version,
                    )
                )
            return predictions

        cached = await self._cache.get_or_compute(cache_keys, compute)
        return PredictFraudOutput(predictions=cast(list[FraudPrediction], cached))

    async def predict_qoe(self, request: PredictQoeInput) -> PredictQoeOutput:
        customer_ids = _deduplicate(request.customer_ids)
        manifest_value = self._features.manifest_for(ModelKind.QOE)
        if not isinstance(manifest_value, QoeFeatureManifest):
            raise _feature_error()
        manifest = manifest_value
        preprocessor = self._features.qoe_preprocessor(manifest)
        feature_maps = await self._fetch_features(
            entity="customers",
            subject_ids=customer_ids,
            alias="qoe_features",
        )
        transformed = {
            customer_id: transform_qoe_features(
                manifest,
                feature_maps[customer_id],
                preprocessor,
            )
            for customer_id in customer_ids
        }
        cache_keys = [
            CacheKey(
                tool_name="predict_qoe",
                subject_id=customer_id,
                model_version=manifest.model_version,
                feature_snapshot=feature_snapshot(transformed[customer_id]),
            )
            for customer_id in customer_ids
        ]

        async def compute(keys: tuple[CacheKey, ...]) -> Sequence[object]:
            rows = [transformed[key.subject_id] for key in keys]
            inference = await self._kserve.infer(ModelKind.QOE, manifest, rows)
            return [
                QoePrediction(
                    customer_id=key.subject_id,
                    score=float(value),
                    model_version=inference.model_version,
                )
                for key, value in zip(keys, inference.values, strict=True)
            ]

        cached = await self._cache.get_or_compute(cache_keys, compute)
        return PredictQoeOutput(predictions=cast(list[QoePrediction], cached))

    async def _fetch_features(
        self,
        *,
        entity: Literal["customers", "transactions"],
        subject_ids: Sequence[str],
        alias: Literal["churn_features", "fraud_features", "qoe_features"],
    ) -> dict[str, Mapping[str, object]]:
        request = FetchCustomerDataInput(
            entity=entity,
            ids=list(subject_ids),
            field_aliases=[alias],
            limit=len(subject_ids),
        )
        try:
            async with asyncio.timeout(_FETCH_DEADLINE_SECONDS):
                response = await self._data_fetch.fetch_customer_data(request)
        except TimeoutError as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_TIMEOUT,
                "The feature service did not respond before the request deadline.",
                retryable=True,
            ) from exc
        except ServiceError as exc:
            if exc.code == ErrorCode.FEATURE_SCHEMA_MISMATCH:
                raise
            if exc.code in {
                ErrorCode.INVALID_FIELD,
                ErrorCode.INVALID_ENTITY,
            }:
                raise _feature_error() from exc
            raise UpstreamError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Source features are temporarily unavailable.",
                retryable=True,
            ) from exc
        except Exception as exc:
            raise UpstreamError(
                ErrorCode.MODEL_UNAVAILABLE,
                "Source features are temporarily unavailable.",
                retryable=True,
            ) from exc

        if response.next_cursor is not None:
            raise _feature_error()

        all_records: dict[str, Mapping[str, object]] = {}
        for record in response.records:
            if record.record_id not in subject_ids or record.record_id in all_records:
                raise _feature_error()
            value = record.attributes.get(alias)
            if not isinstance(value, Mapping):
                raise _feature_error()
            all_records[record.record_id] = cast(Mapping[str, object], value)

        missing_ids = [subject_id for subject_id in subject_ids if subject_id not in all_records]
        if missing_ids:
            raise ServiceError(
                ErrorCode.SUBJECT_NOT_FOUND,
                "One or more requested records or assigned feature samples were not found.",
                retryable=False,
                details={"missing_count": len(missing_ids)},
            )
        return all_records


def create_mcp_server(service: PredictionService) -> FastMCP:
    """Register the three stable tools with explicit frozen input/output schemas."""

    async def predict_churn(customer_ids: object) -> PredictChurnOutput | ToolResult:
        async def run() -> PredictChurnOutput:
            request = _parse_input(PredictChurnInput, {"customer_ids": customer_ids})
            return await service.predict_churn(request)

        return await _invoke_tool(run)

    async def predict_fraud(transaction_ids: object) -> PredictFraudOutput | ToolResult:
        async def run() -> PredictFraudOutput:
            request = _parse_input(PredictFraudInput, {"transaction_ids": transaction_ids})
            return await service.predict_fraud(request)

        return await _invoke_tool(run)

    async def predict_qoe(customer_ids: object) -> PredictQoeOutput | ToolResult:
        async def run() -> PredictQoeOutput:
            request = _parse_input(PredictQoeInput, {"customer_ids": customer_ids})
            return await service.predict_qoe(request)

        return await _invoke_tool(run)

    tools = [
        FunctionTool(
            name="predict_churn",
            description="Predict the raw churn class for each customer's latest billing cycle.",
            fn=predict_churn,
            parameters=PredictChurnInput.model_json_schema(),
            output_schema=PredictChurnOutput.model_json_schema(),
            return_type=PredictChurnOutput,
            run_in_thread=False,
        ),
        FunctionTool(
            name="predict_fraud",
            description="Classify each requested RAFM transaction as Non-Fraud or Fraud.",
            fn=predict_fraud,
            parameters=PredictFraudInput.model_json_schema(),
            output_schema=PredictFraudOutput.model_json_schema(),
            return_type=PredictFraudOutput,
            run_in_thread=False,
        ),
        FunctionTool(
            name="predict_qoe",
            description="Return each customer's QoE score for the assigned location sample.",
            fn=predict_qoe,
            parameters=PredictQoeInput.model_json_schema(),
            output_schema=PredictQoeOutput.model_json_schema(),
            return_type=PredictQoeOutput,
            run_in_thread=False,
        ),
    ]
    return FastMCP("telco-prediction-tools", tools=tools, mask_error_details=True)


def create_default_server() -> FastMCP:
    """Build the production server from required environment-backed settings."""

    settings = PredictionSettings()
    data_fetch_client = _create_data_fetch_client(str(settings.data_fetch_mcp_url))
    endpoint_map = {
        ModelKind.CHURN: KServeEndpoint(
            url=str(settings.churn_model_url), bearer_token=settings.churn_model_auth_token
        ),
        ModelKind.FRAUD: KServeEndpoint(
            url=str(settings.fraud_model_url), bearer_token=settings.fraud_model_auth_token
        ),
        ModelKind.QOE: KServeEndpoint(
            url=str(settings.qoe_model_url), bearer_token=settings.qoe_model_auth_token
        ),
    }
    service = PredictionService(
        data_fetch_client=data_fetch_client,
        features=FeatureRegistry(settings.model_feature_manifest_dir),
        kserve_client=KServeV2Client(endpoint_map),
    )
    return create_mcp_server(service)


def main() -> None:
    """Run this service over FastMCP streamable HTTP."""

    configure_logging()
    create_default_server().run(transport="http")


def _create_data_fetch_client(endpoint: str) -> DataFetchClient:
    from fastmcp import Client

    class McpDataFetchClient:
        """Short-lived MCP sessions avoid retaining stale upstream tool definitions."""

        async def fetch_customer_data(
            self, request: FetchCustomerDataInput
        ) -> FetchCustomerDataOutput:
            try:
                async with asyncio.timeout(_FETCH_DEADLINE_SECONDS):
                    async with Client(endpoint, timeout=_FETCH_DEADLINE_SECONDS) as client:
                        available_tools = await client.list_tools()
                        if not any(tool.name == "fetch_customer_data" for tool in available_tools):
                            raise UpstreamError(
                                ErrorCode.MODEL_UNAVAILABLE,
                                "The source feature service is temporarily unavailable.",
                                retryable=True,
                            )
                        result = await client.call_tool(
                            "fetch_customer_data",
                            request.to_wire(),
                            raise_on_error=False,
                        )
            except TimeoutError as exc:
                raise UpstreamError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    "The feature service did not respond before the request deadline.",
                    retryable=True,
                ) from exc
            except ServiceError:
                raise
            except Exception as exc:
                raise UpstreamError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    "Source features are temporarily unavailable.",
                    retryable=True,
                ) from exc
            if result.is_error:
                code = (
                    result.structured_content.get("code")
                    if isinstance(result.structured_content, dict)
                    else None
                )
                if code == "DATABASE_UNAVAILABLE":
                    raise UpstreamError(
                        ErrorCode.MODEL_UNAVAILABLE,
                        "Source features are temporarily unavailable.",
                        retryable=True,
                    )
                raise _feature_error()
            structured = result.structured_content
            if not isinstance(structured, dict):
                raise _feature_error()
            try:
                return FetchCustomerDataOutput.model_validate(structured)
            except ValidationError as exc:
                raise _feature_error() from exc

    return cast(DataFetchClient, McpDataFetchClient())


def _parse_input[InputModel: BaseModel](model_type: type[InputModel], data: object) -> InputModel:
    try:
        return model_type.model_validate(data)
    except ValidationError as exc:
        raise RequestError(
            ErrorCode.INVALID_INPUT,
            "Prediction identifiers must be a non-empty batch of at most 100 valid IDs.",
            retryable=False,
        ) from exc


async def _invoke_tool[OutputModel: BaseModel](
    operation: Callable[[], Awaitable[OutputModel]],
) -> OutputModel | ToolResult:
    request_id = get_request_id() or f"req_{uuid4().hex[:12]}"
    with bind_request_id(request_id):
        try:
            return await operation()
        except ServiceError as exc:
            payload = exc.to_mcp_payload(request_id)
        except Exception as exc:
            logger.error(
                "prediction tool failed",
                extra={"request_id": request_id, "error_type": type(exc).__name__},
            )
            payload = ServiceError(
                ErrorCode.MODEL_UNAVAILABLE,
                "The prediction could not be completed.",
                retryable=False,
            ).to_mcp_payload(request_id)
    return ToolResult(
        content=[TextContent(type="text", text=payload.message)],
        structured_content=payload.to_wire(),
        is_error=True,
    )


def _deduplicate(subject_ids: Sequence[str]) -> list[str]:
    if not 1 <= len(subject_ids) <= 100 or any(
        not isinstance(subject_id, str) or not 1 <= len(subject_id) <= 200
        for subject_id in subject_ids
    ):
        raise RequestError(
            ErrorCode.INVALID_INPUT,
            "Prediction identifiers must be a non-empty batch of at most 100 valid IDs.",
            retryable=False,
        )
    return list(dict.fromkeys(subject_ids))


def _feature_error() -> ServiceError:
    return ServiceError(
        ErrorCode.FEATURE_SCHEMA_MISMATCH,
        "The source features do not match the deployed model contract.",
        retryable=False,
    )


if __name__ == "__main__":
    main()
