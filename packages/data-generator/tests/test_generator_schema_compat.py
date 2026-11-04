from __future__ import annotations

import re
from pathlib import Path

from data_generator.writer import TABLES, TableName

_WRITER_TABLE_COLUMNS: dict[TableName, tuple[str, ...]] = {
    "customers": (
        "customer_id",
        "plan_type",
        "dollar_weight",
        "region_id",
        "source_dataset_revision",
        "source_features",
        "created_at",
        "updated_at",
    ),
    "billing_cycles": (
        "billing_cycle_id",
        "customer_id",
        "billing_cycle_start",
        "total_bill_amount",
        "source_dataset_revision",
        "source_features",
        "created_at",
    ),
    "transactions": (
        "transaction_id",
        "customer_id",
        "plan_type",
        "is_fraud",
        "source_dataset_revision",
        "source_features",
        "created_at",
    ),
    "qoe_samples": (
        "qoe_sample_id",
        "customer_id",
        "region_id",
        "latitude",
        "longitude",
        "elevation_m",
        "visible_satellites",
        "serving_satellites",
        "signal_loss_db",
        "season",
        "weather",
        "source_dataset_revision",
        "source_features",
        "created_at",
    ),
}
_CONSTRAINT_KEYWORDS = {"CHECK", "CONSTRAINT", "FOREIGN", "PRIMARY", "UNIQUE"}


def _contract_table_columns() -> dict[str, tuple[str, ...]]:
    schema_path = Path(__file__).parents[3] / "contracts" / "schema.sql"
    schema = schema_path.read_text(encoding="utf-8")
    columns: dict[str, tuple[str, ...]] = {}
    for table_name in _WRITER_TABLE_COLUMNS:
        pattern = rf"CREATE TABLE IF NOT EXISTS app\.{re.escape(table_name)}\s*\((.*?)\n\);"
        match = re.search(pattern, schema, flags=re.DOTALL)
        assert match is not None, f"missing app.{table_name} in contracts/schema.sql"
        names = []
        for line in match.group(1).splitlines():
            declaration = line.strip()
            if not declaration:
                continue
            name = declaration.split(maxsplit=1)[0].rstrip(",")
            if name.upper() not in _CONSTRAINT_KEYWORDS:
                names.append(name)
        columns[table_name] = tuple(names)
    return columns


def test_writer_tables_match_every_column_in_the_frozen_schema() -> None:
    metadata_columns = {
        table_name: tuple(TABLES[table_name].columns.keys()) for table_name in _WRITER_TABLE_COLUMNS
    }

    assert metadata_columns == _WRITER_TABLE_COLUMNS
    assert _contract_table_columns() == _WRITER_TABLE_COLUMNS
