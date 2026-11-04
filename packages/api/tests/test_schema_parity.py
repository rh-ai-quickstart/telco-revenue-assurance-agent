from __future__ import annotations

import hashlib
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from api.db.models import Base
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import CHAR

REPOSITORY_ROOT = Path(__file__).parents[3]
SCHEMA_PATH = REPOSITORY_ROOT / "contracts" / "schema.sql"
MIGRATION_PATH = (
    REPOSITORY_ROOT / "packages" / "api" / "alembic" / "versions" / "0001_initial_schema.py"
)


def test_migration_sql_is_byte_for_byte_the_approved_schema() -> None:
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")
    migration_namespace = runpy.run_path(str(MIGRATION_PATH))

    assert migration_namespace["SCHEMA_SQL"] == schema_text
    assert migration_namespace["SCHEMA_SHA256"] == hashlib.sha256(schema_text.encode()).hexdigest()


def test_chat_request_model_matches_durable_idempotency_contract() -> None:
    table = Base.metadata.tables["app.chat_requests"]

    assert set(table.columns.keys()) == {
        "session_id",
        "request_id",
        "message_sha256",
        "status",
        "response_payload",
        "created_at",
        "completed_at",
    }
    assert [column.name for column in table.primary_key.columns] == [
        "session_id",
        "request_id",
    ]
    message_hash_type = table.columns["message_sha256"].type
    assert isinstance(message_hash_type, CHAR)
    assert message_hash_type.length == 64
    assert table.columns["response_payload"].nullable is True
    status_constraints = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert "status IN ('in_progress', 'completed', 'rejected')" in status_constraints
    assert any("status IN ('in_progress', 'rejected')" in text for text in status_constraints)


def test_migration_statement_splitter_preserves_dollar_quoted_blocks() -> None:
    migration_namespace: dict[str, Any] = runpy.run_path(str(MIGRATION_PATH))
    splitter = cast(Callable[[str], list[str]], migration_namespace["_split_sql_statements"])

    statements = splitter(
        "CREATE TABLE app.example (value TEXT);\n"
        "DO $$ BEGIN PERFORM 'semi;colon'; END $$;\n"
        "COMMENT ON TABLE app.example IS 'ready';"
    )

    assert len(statements) == 3
    assert statements[1] == "DO $$ BEGIN PERFORM 'semi;colon'; END $$"
