"""Typed input and output models for the frozen data-fetch MCP contract."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from common.models import CommonModel

Entity = Literal["customers", "billing_cycles", "transactions", "qoe_samples", "predictions"]
FieldAlias = Literal[
    "customer_id",
    "transaction_id",
    "billing_cycle_id",
    "qoe_sample_id",
    "prediction_id",
    "scoring_run_id",
    "plan_type",
    "dollar_weight",
    "region_id",
    "billing_cycle_start",
    "total_bill_amount",
    "churn_features",
    "fraud_features",
    "qoe_features",
    "model_type",
    "class_id",
    "is_fraud",
    "score",
    "model_version",
]
ScalarValue = str | int | float | bool | None
_FilterValues = Annotated[list[ScalarValue], Field(min_length=1, max_length=100)]
FilterValue = ScalarValue | _FilterValues

ENTITY_VALUES: tuple[str, ...] = (
    "customers",
    "billing_cycles",
    "transactions",
    "qoe_samples",
    "predictions",
)
FIELD_ALIAS_VALUES: tuple[str, ...] = (
    "customer_id",
    "transaction_id",
    "billing_cycle_id",
    "qoe_sample_id",
    "prediction_id",
    "scoring_run_id",
    "plan_type",
    "dollar_weight",
    "region_id",
    "billing_cycle_start",
    "total_bill_amount",
    "churn_features",
    "fraud_features",
    "qoe_features",
    "model_type",
    "class_id",
    "is_fraud",
    "score",
    "model_version",
)

_EntityInput = Annotated[
    str,
    Field(
        min_length=1,
        max_length=32,
        json_schema_extra={"enum": list(ENTITY_VALUES)},
    ),
]
_AliasInput = Annotated[
    str,
    Field(
        min_length=1,
        max_length=32,
        json_schema_extra={"enum": list(FIELD_ALIAS_VALUES)},
    ),
]
_StableId = Annotated[str, Field(min_length=1, max_length=200)]


class FilterInput(CommonModel):
    """One comparison on a public field alias."""

    field: _AliasInput
    operator: Annotated[
        str,
        Field(json_schema_extra={"enum": ["eq", "in", "gte", "lte"]}),
    ]
    value: FilterValue


class SortInput(CommonModel):
    """One public alias and sort direction."""

    field: _AliasInput
    direction: Annotated[
        str,
        Field(json_schema_extra={"enum": ["asc", "desc"]}),
    ]


class FetchCustomerDataInput(CommonModel):
    """Bounded, typed arguments accepted by the service client."""

    entity: _EntityInput
    ids: list[_StableId] | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        json_schema_extra={"uniqueItems": True},
    )
    field_aliases: list[_AliasInput] | None = Field(
        default=None,
        max_length=19,
        json_schema_extra={"uniqueItems": True},
    )
    filters: list[FilterInput] | None = Field(default=None, min_length=1, max_length=10)
    sort: SortInput | None = None
    limit: int = Field(ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_selector(self) -> "FetchCustomerDataInput":
        if self.ids is None and self.filters is None:
            raise ValueError("Provide at least one of ids or filters.")
        if self.ids is not None and len(self.ids) != len(set(self.ids)):
            raise ValueError("ids must contain unique values.")
        if self.field_aliases is not None and len(self.field_aliases) != len(
            set(self.field_aliases)
        ):
            raise ValueError("field_aliases must contain unique values.")
        return self


class FetchRecord(CommonModel):
    """One entity-scoped record with public aliases only."""

    entity: Entity
    record_id: str = Field(min_length=1)
    attributes: dict[FieldAlias, ScalarValue | dict[str, ScalarValue]]


class FetchCustomerDataOutput(CommonModel):
    """Bounded typed result with an opaque cursor for the next page."""

    records: list[FetchRecord] = Field(max_length=100)
    next_cursor: str | None = Field(max_length=1024)
