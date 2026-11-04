"""Ensure SQLAlchemy metadata consumes the full frozen database schema."""

import json
import re
from pathlib import Path
from typing import cast

import pytest
from data_fetch_tool import FetchCustomerDataInput
from data_fetch_tool.field_allowlist import (
    ENTITY_TABLES,
    FIELD_ALLOWLIST,
    FILTER_ONLY_FIELDS,
)
from data_fetch_tool.repository import prepare_request
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.type_api import TypeEngine

from common.errors import ErrorCode, RequestError

_TABLE_PATTERN = re.compile(
    r"CREATE TABLE IF NOT EXISTS app\.(?P<name>[a-z_]+)\s*\((?P<body>.*?)\n\);",
    re.DOTALL,
)
_COLUMN_PATTERN = re.compile(
    r"^(?P<name>[a-z_][a-z0-9_]*)\s+"
    r"(?P<type>(?i:DOUBLE PRECISION|NUMERIC\([^)]*\)|CHAR\([^)]*\)|"
    r"APP\.[A-Z0-9_]+|[A-Z][A-Z0-9_]*))"
    r"(?=\s|$)(?P<constraints>.*)$",
)


def _postgresql_dialect() -> Dialect:
    return cast(Dialect, postgresql.dialect())  # type: ignore[no-untyped-call]


def _contract_columns(schema_sql: str, table_name: str) -> dict[str, tuple[str, bool, bool]]:
    table_match = next(
        match for match in _TABLE_PATTERN.finditer(schema_sql) if match.group("name") == table_name
    )
    columns: dict[str, tuple[str, bool, bool]] = {}
    for line in table_match.group("body").splitlines():
        match = _COLUMN_PATTERN.match(line.strip().rstrip(","))
        if match is None:
            continue
        constraints = match.group("constraints").upper()
        nullable = "NOT NULL" not in constraints and "PRIMARY KEY" not in constraints
        primary_key = "PRIMARY KEY" in constraints
        sql_type = re.sub(r"\s+", " ", match.group("type").upper())
        if sql_type == "TIMESTAMPTZ":
            sql_type = "TIMESTAMP WITH TIME ZONE"
        columns[match.group("name")] = (sql_type, nullable, primary_key)
    return columns


def _metadata_type(column_type: TypeEngine[object]) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(column_type.compile(dialect=_postgresql_dialect())).replace('"', "").upper(),
    )


def test_entity_tables_match_every_column_in_the_frozen_ddl() -> None:
    schema_sql = Path("contracts/schema.sql").read_text(encoding="utf-8")
    expected_entities = {
        "customers",
        "billing_cycles",
        "transactions",
        "qoe_samples",
        "predictions",
    }
    assert set(ENTITY_TABLES) == expected_entities

    for table_name, table in ENTITY_TABLES.items():
        expected = _contract_columns(schema_sql, table_name)
        actual = {column.name: column for column in table.columns}
        assert set(actual) == set(expected), table_name
        for column_name, column in actual.items():
            expected_type, expected_nullable, expected_primary_key = expected[column_name]
            assert _metadata_type(column.type) == expected_type, f"{table_name}.{column_name} type"
            assert column.nullable is expected_nullable, f"{table_name}.{column_name} nullability"
            assert column.primary_key is expected_primary_key, (
                f"{table_name}.{column_name} primary key"
            )


def test_entity_field_aliases_match_the_frozen_mcp_contract() -> None:
    contract = json.loads(Path("contracts/data-fetch-mcp.json").read_text(encoding="utf-8"))
    contract_aliases = contract["x-entity-field-map"]

    assert set(contract_aliases) == set(FIELD_ALLOWLIST)
    for entity, aliases in contract_aliases.items():
        assert set(aliases) == set(FIELD_ALLOWLIST[entity])


def test_fraud_label_can_filter_but_cannot_be_selected() -> None:
    contract = json.loads(Path("contracts/data-fetch-mcp.json").read_text(encoding="utf-8"))
    contract_filter_aliases = contract["x-entity-filter-only-field-map"]
    source_filter_aliases = {
        entity: sorted(fields) for entity, fields in FILTER_ONLY_FIELDS.items()
    }
    assert contract_filter_aliases == source_filter_aliases

    filters = [{"field": "is_fraud", "operator": "in", "value": [True, False]}]
    request = FetchCustomerDataInput.model_validate(
        {
            "entity": "transactions",
            "field_aliases": ["transaction_id"],
            "filters": filters,
            "limit": 100,
        }
    )
    prepared = prepare_request(request, 100)
    assert [(item.alias, item.operator, item.value) for item in prepared.filters] == [
        ("is_fraud", "in", [True, False])
    ]

    target_selection = FetchCustomerDataInput.model_validate(
        {
            "entity": "transactions",
            "field_aliases": ["is_fraud"],
            "filters": filters,
            "limit": 100,
        }
    )
    with pytest.raises(RequestError) as exc_info:
        prepare_request(target_selection, 100)
    assert exc_info.value.code == ErrorCode.INVALID_FIELD
