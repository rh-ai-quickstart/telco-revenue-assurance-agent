"""Initial migration for the approved provisional schema."""

# The embedded contract SQL must remain byte-for-byte identical to contracts/schema.sql.
# ruff: noqa: E501, RUF001
from __future__ import annotations

import re

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA_SHA256 = "d7bae521eb4f09eca4f701bb6be1ba18572109ce257463cac4efd8bc97a275be"
SCHEMA_SQL = r"""-- Contract: database-schema v1, owner backend, consumers data-fetch-tool and data-generator.
-- Side effects: this DDL is applied only by the versioned migration job, never at request or application startup.
-- Errors: constraint failures roll back the current transaction; callers map them to typed errors and never return raw driver text.
-- Timeouts/retries: migrations set finite lock and statement timeouts; the seed worker uses bounded idempotent upserts.
-- Ordering: migrations apply in revision order; seed and API work that needs this schema starts only after the migration head is current.
-- Versioning: Alembic revisions are additive/expand-migrate-contract for destructive changes.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS app;

DO $$ BEGIN
    CREATE TYPE app.scoring_run_status AS ENUM ('queued', 'running', 'completed', 'failed');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE app.model_family AS ENUM ('churn', 'fraud', 'qoe');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS app.customers (
    customer_id TEXT PRIMARY KEY CHECK (length(btrim(customer_id)) > 0),
    plan_type TEXT NULL CHECK (plan_type IS NULL OR length(btrim(plan_type)) > 0),
    dollar_weight NUMERIC(18, 6) NOT NULL CHECK (dollar_weight >= 0),
    region_id TEXT NOT NULL CHECK (length(btrim(region_id)) > 0),
    source_dataset_revision TEXT NOT NULL CHECK (length(btrim(source_dataset_revision)) > 0),
    source_features JSONB NOT NULL CHECK (jsonb_typeof(source_features) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.customers IS
    'One row per CustomerID. dollar_weight is the mean TotalBillAmount across every retained billing cycle.';
COMMENT ON COLUMN app.customers.source_features IS
    'Lossless JSONB source/profile features not promoted to stable typed columns.';

CREATE TABLE IF NOT EXISTS app.billing_cycles (
    billing_cycle_id TEXT PRIMARY KEY CHECK (length(btrim(billing_cycle_id)) > 0),
    customer_id TEXT NOT NULL REFERENCES app.customers(customer_id) ON DELETE RESTRICT,
    billing_cycle_start DATE NOT NULL,
    total_bill_amount NUMERIC(18, 6) NOT NULL CHECK (total_bill_amount >= 0),
    source_dataset_revision TEXT NOT NULL CHECK (length(btrim(source_dataset_revision)) > 0),
    source_features JSONB NOT NULL CHECK (jsonb_typeof(source_features) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (customer_id, billing_cycle_start, billing_cycle_id)
);

COMMENT ON TABLE app.billing_cycles IS
    'One row for every source churn billing-cycle row; HasChurned and all original source fields remain in source_features.';

CREATE TABLE IF NOT EXISTS app.transactions (
    transaction_id TEXT PRIMARY KEY CHECK (length(btrim(transaction_id)) > 0),
    customer_id TEXT NOT NULL REFERENCES app.customers(customer_id) ON DELETE RESTRICT,
    plan_type TEXT NOT NULL CHECK (length(btrim(plan_type)) > 0),
    is_fraud BOOLEAN NOT NULL,
    source_dataset_revision TEXT NOT NULL CHECK (length(btrim(source_dataset_revision)) > 0),
    source_features JSONB NOT NULL CHECK (jsonb_typeof(source_features) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.transactions IS
    'One row for every source RAFM transaction. transaction_id is a stable loader key; source fields and label are retained.';

CREATE TABLE IF NOT EXISTS app.qoe_samples (
    qoe_sample_id TEXT PRIMARY KEY CHECK (length(btrim(qoe_sample_id)) > 0),
    customer_id TEXT NOT NULL REFERENCES app.customers(customer_id) ON DELETE RESTRICT,
    region_id TEXT NOT NULL CHECK (length(btrim(region_id)) > 0),
    latitude DOUBLE PRECISION NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude DOUBLE PRECISION NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    elevation_m DOUBLE PRECISION NOT NULL,
    visible_satellites INTEGER NOT NULL CHECK (visible_satellites >= 0),
    serving_satellites INTEGER NOT NULL CHECK (serving_satellites >= 0),
    signal_loss_db DOUBLE PRECISION NOT NULL,
    season TEXT NOT NULL CHECK (length(btrim(season)) > 0),
    weather TEXT NOT NULL CHECK (length(btrim(weather)) > 0),
    source_dataset_revision TEXT NOT NULL CHECK (length(btrim(source_dataset_revision)) > 0),
    source_features JSONB NOT NULL CHECK (jsonb_typeof(source_features) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.qoe_samples IS
    'One row for every Starlink source sample, assigned to a synthetic customer and region; raw features are retained.';

CREATE TABLE IF NOT EXISTS app.scoring_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(btrim(idempotency_key)) > 0),
    request_fingerprint CHAR(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    status app.scoring_run_status NOT NULL DEFAULT 'queued',
    churn_processed_rows BIGINT NOT NULL DEFAULT 0 CHECK (churn_processed_rows >= 0),
    churn_total_rows BIGINT NOT NULL DEFAULT 0 CHECK (churn_total_rows >= churn_processed_rows),
    fraud_processed_rows BIGINT NOT NULL DEFAULT 0 CHECK (fraud_processed_rows >= 0),
    fraud_total_rows BIGINT NOT NULL DEFAULT 0 CHECK (fraud_total_rows >= fraud_processed_rows),
    qoe_processed_rows BIGINT NOT NULL DEFAULT 0 CHECK (qoe_processed_rows >= 0),
    qoe_total_rows BIGINT NOT NULL DEFAULT 0 CHECK (qoe_total_rows >= qoe_processed_rows),
    error_code TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
      (status IN ('queued', 'running') AND completed_at IS NULL)
      OR (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
    )
);

COMMENT ON TABLE app.scoring_runs IS
    'Explicit analyst-triggered run; one active run is admitted at a time and typed per-model progress counters are monotonic.';

CREATE TABLE IF NOT EXISTS app.chat_requests (
    session_id UUID NOT NULL,
    request_id UUID NOT NULL,
    message_sha256 CHAR(64) NOT NULL CHECK (message_sha256 ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (
        status IN ('in_progress', 'completed', 'rejected')
    ),
    response_payload JSONB NULL CHECK (
        response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NULL,
    PRIMARY KEY (session_id, request_id),
    CHECK (
        (status IN ('in_progress', 'rejected') AND response_payload IS NULL AND completed_at IS NULL)
        OR
        (status = 'completed' AND response_payload IS NOT NULL AND completed_at IS NOT NULL)
    )
);

COMMENT ON TABLE app.chat_requests IS
    'Durable backend idempotency records scoped to an OGX conversation; binds a request ID to a message hash across explicit rejections and stores completed public responses.';

CREATE TABLE IF NOT EXISTS app.predictions (
    prediction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scoring_run_id UUID NOT NULL REFERENCES app.scoring_runs(run_id) ON DELETE CASCADE,
    model_type app.model_family NOT NULL,
    customer_id TEXT NULL REFERENCES app.customers(customer_id) ON DELETE RESTRICT,
    transaction_id TEXT NULL REFERENCES app.transactions(transaction_id) ON DELETE RESTRICT,
    class_id BIGINT NULL,
    is_fraud BOOLEAN NULL,
    score NUMERIC(8, 4) NULL CHECK (score IS NULL OR score BETWEEN 0 AND 100),
    model_version TEXT NOT NULL CHECK (length(btrim(model_version)) > 0),
    feature_snapshot_sha256 CHAR(64) NOT NULL CHECK (feature_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    model_output JSONB NOT NULL CHECK (jsonb_typeof(model_output) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
      (model_type = 'churn' AND customer_id IS NOT NULL AND transaction_id IS NULL
        AND class_id IS NOT NULL AND is_fraud IS NULL AND score IS NULL)
      OR
      (model_type = 'fraud' AND customer_id IS NULL AND transaction_id IS NOT NULL
        AND class_id IS NOT NULL AND class_id IN (0, 1) AND is_fraud IS NOT NULL
        AND is_fraud = (class_id = 1) AND score IS NULL)
      OR
      (model_type = 'qoe' AND customer_id IS NOT NULL AND transaction_id IS NULL
        AND class_id IS NULL AND is_fraud IS NULL AND score IS NOT NULL)
    )
);

COMMENT ON TABLE app.predictions IS
    'One prediction per run/model/subject. Churn keeps the artifact class ID raw; fraud maps class order [False, True]; QoE stores a 0–100 score.';

CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_run_model_customer
    ON app.predictions (scoring_run_id, model_type, customer_id)
    WHERE customer_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_run_model_transaction
    ON app.predictions (scoring_run_id, model_type, transaction_id)
    WHERE transaction_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_customers_region
    ON app.customers (region_id, customer_id);

CREATE INDEX IF NOT EXISTS ix_billing_cycles_customer_latest
    ON app.billing_cycles (customer_id, billing_cycle_start DESC, billing_cycle_id);

CREATE INDEX IF NOT EXISTS ix_transactions_customer
    ON app.transactions (customer_id, transaction_id);

CREATE INDEX IF NOT EXISTS ix_transactions_fraud
    ON app.transactions (is_fraud, transaction_id);

CREATE INDEX IF NOT EXISTS ix_qoe_samples_customer
    ON app.qoe_samples (customer_id, qoe_sample_id);

CREATE INDEX IF NOT EXISTS ix_scoring_runs_created
    ON app.scoring_runs (created_at DESC, run_id);

CREATE INDEX IF NOT EXISTS ix_scoring_runs_status
    ON app.scoring_runs (status, created_at);

CREATE INDEX IF NOT EXISTS ix_predictions_run_model_subject
    ON app.predictions (scoring_run_id, model_type, customer_id, transaction_id);

CREATE INDEX IF NOT EXISTS ix_predictions_created
    ON app.predictions (created_at DESC, prediction_id);
"""


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")


def _split_sql_statements(sql: str) -> list[str]:
    """Split PostgreSQL DDL on semicolons outside strings, comments, and dollar quotes."""

    statements: list[str] = []
    current: list[str] = []
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False

    while index < len(sql):
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
            else:
                current.append(sql[index])
                index += 1
            continue

        character = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""

        if line_comment:
            current.append(character)
            index += 1
            if character == "\n":
                line_comment = False
            continue

        if block_comment:
            current.append(character)
            index += 1
            if character == "*" and following == "/":
                current.append(following)
                index += 1
                block_comment = False
            continue

        if quote is not None:
            current.append(character)
            index += 1
            if character == quote:
                if index < len(sql) and sql[index] == quote:
                    current.append(sql[index])
                    index += 1
                else:
                    quote = None
            continue

        if character == "-" and following == "-":
            current.extend((character, following))
            index += 2
            line_comment = True
            continue
        if character == "/" and following == "*":
            current.extend((character, following))
            index += 2
            block_comment = True
            continue
        if character in {"'", '"'}:
            current.append(character)
            quote = character
            index += 1
            continue
        if character == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match is not None:
                dollar_tag = match.group()
                current.append(dollar_tag)
                index += len(dollar_tag)
                continue
        if character == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current.clear()
            index += 1
            continue

        current.append(character)
        index += 1

    trailing_statement = "".join(current).strip()
    if trailing_statement:
        statements.append(trailing_statement)
    return statements


def upgrade() -> None:
    for statement in _split_sql_statements(SCHEMA_SQL):
        if not all(
            line.lstrip().startswith("--") for line in statement.splitlines() if line.strip()
        ):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.predictions CASCADE")
    op.execute("DROP TABLE IF EXISTS app.chat_requests CASCADE")
    op.execute("DROP TABLE IF EXISTS app.scoring_runs CASCADE")
    op.execute("DROP TABLE IF EXISTS app.qoe_samples CASCADE")
    op.execute("DROP TABLE IF EXISTS app.transactions CASCADE")
    op.execute("DROP TABLE IF EXISTS app.billing_cycles CASCADE")
    op.execute("DROP TABLE IF EXISTS app.customers CASCADE")
    op.execute("DROP TYPE IF EXISTS app.model_family")
    op.execute("DROP TYPE IF EXISTS app.scoring_run_status")
    op.execute("DROP SCHEMA IF EXISTS app")
