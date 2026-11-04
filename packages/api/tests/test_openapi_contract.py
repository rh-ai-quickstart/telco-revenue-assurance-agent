from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from api.main import create_app

REPOSITORY_ROOT = Path(__file__).parents[3]
CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "openapi.yaml"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _resolve(document: Mapping[str, Any], reference: str) -> Any:  # noqa: ANN401
    assert reference.startswith("#/"), f"unsupported OpenAPI reference: {reference}"
    value: Any = document
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[token]
    return value


def _resolve_object(document: Mapping[str, Any], value: Any) -> Any:  # noqa: ANN401
    while isinstance(value, dict) and isinstance(value.get("$ref"), str):
        value = _resolve(document, value["$ref"])
    return value


def _schema_shape(document: Mapping[str, Any], schema: Any) -> Any:  # noqa: ANN401
    schema = _resolve_object(document, schema)
    if not isinstance(schema, dict):
        return schema

    shape: dict[str, Any] = {}
    for key in (
        "type",
        "format",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "additionalProperties",
    ):
        if key not in schema:
            continue
        value = schema[key]
        if key == "additionalProperties" and isinstance(value, dict):
            value = _schema_shape(document, value)
        shape[key] = value
    if "required" in schema:
        shape["required"] = sorted(schema["required"])
    if "properties" in schema:
        properties = schema["properties"]
        shape["properties"] = {
            name: _schema_shape(document, property_schema)
            for name, property_schema in sorted(properties.items())
        }
    if "items" in schema:
        shape["items"] = _schema_shape(document, schema["items"])
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            alternatives = [_schema_shape(document, item) for item in schema[key]]
            shape[key] = sorted(alternatives, key=repr)
    return shape


def _operation_map(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    paths = document["paths"]
    return {
        f"{path} {method}": operation
        for path, path_item in paths.items()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS
    }


def test_openapi_metadata_and_operations_match_the_frozen_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    served = create_app().openapi()

    assert served["openapi"] == contract["openapi"]
    assert served["info"] == contract["info"]
    assert served["servers"] == contract["servers"]
    assert served["security"] == contract["security"]
    assert served["tags"] == contract["tags"]
    assert served["x-contract-guarantees"] == contract["x-contract-guarantees"]
    assert served["components"]["securitySchemes"] == contract["components"]["securitySchemes"]
    assert served["paths"].keys() == contract["paths"].keys()

    contract_operations = _operation_map(contract)
    served_operations = _operation_map(served)
    assert served_operations.keys() == contract_operations.keys()
    for operation_key, expected in contract_operations.items():
        actual = served_operations[operation_key]
        assert actual["operationId"] == expected["operationId"]
        assert actual.get("x-side-effects") == expected.get("x-side-effects")
        assert actual.get("x-error-codes") == expected.get("x-error-codes")
        assert actual.get("x-timeouts-and-retries") == expected.get("x-timeouts-and-retries")
        assert actual.get("x-ordering") == expected.get("x-ordering")
        assert actual.get("x-versioning") == expected.get("x-versioning")
        assert actual["responses"].keys() == expected["responses"].keys()


def test_openapi_parameters_and_wire_schemas_match_the_frozen_contract() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    served = create_app().openapi()
    contract_operations = _operation_map(contract)
    served_operations = _operation_map(served)

    for operation_key, expected in contract_operations.items():
        actual = served_operations[operation_key]
        expected_parameters = [
            _resolve_object(contract, item) for item in expected.get("parameters", [])
        ]
        actual_parameters = actual.get("parameters", [])
        expected_parameter_shapes = sorted(
            (
                item["name"],
                item["in"],
                item.get("required", False),
                _schema_shape(contract, item["schema"]),
            )
            for item in expected_parameters
        )
        actual_parameter_shapes = sorted(
            (
                item["name"],
                item["in"],
                item.get("required", False),
                _schema_shape(served, item["schema"]),
            )
            for item in actual_parameters
        )
        assert actual_parameter_shapes == expected_parameter_shapes, operation_key

        expected_request = expected.get("requestBody")
        actual_request = actual.get("requestBody")
        assert (expected_request is None) == (actual_request is None), operation_key
        if expected_request is not None and actual_request is not None:
            expected_request = _resolve_object(contract, expected_request)
            actual_request = _resolve_object(served, actual_request)
            assert actual_request.get("required", False) == expected_request.get("required", False)
            assert actual_request["content"].keys() == expected_request["content"].keys()
            for content_type, expected_content in expected_request["content"].items():
                actual_content = actual_request["content"][content_type]
                assert _schema_shape(served, actual_content["schema"]) == _schema_shape(
                    contract, expected_content["schema"]
                ), operation_key

        for status_code, expected_response in expected["responses"].items():
            expected_response = _resolve_object(contract, expected_response)
            actual_response = _resolve_object(served, actual["responses"][status_code])
            expected_content = expected_response.get("content", {})
            actual_content = actual_response.get("content", {})
            assert actual_content.keys() == expected_content.keys(), (operation_key, status_code)
            for content_type, expected_media in expected_content.items():
                assert _schema_shape(
                    served, actual_content[content_type]["schema"]
                ) == _schema_shape(contract, expected_media["schema"]), (operation_key, status_code)
