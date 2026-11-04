"""Shared Pydantic model and wire serialization policy."""

from typing import cast

from pydantic import BaseModel, ConfigDict


class CommonModel(BaseModel):
    """Pydantic base with strict fields and stable JSON-compatible serialization."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        use_enum_values=True,
        validate_assignment=True,
    )

    def to_wire(self) -> dict[str, object]:
        """Serialize aliases, enum values, ISO-8601 datetimes, and explicit nulls."""

        wire_data = self.model_dump(mode="json", by_alias=True, exclude_none=False)
        return cast(dict[str, object], wire_data)


__all__ = ["CommonModel"]
