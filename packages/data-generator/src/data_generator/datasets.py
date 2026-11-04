"""Pinned Hugging Face dataset snapshots used by the finite seed worker."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from datasets import load_dataset  # type: ignore[import-untyped]
from huggingface_hub import HfApi


@dataclass(frozen=True, slots=True)
class DatasetSource:
    name: str
    dataset_id: str
    revision: str = "main"
    split: str = "train"


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    source: DatasetSource
    revision: str
    rows: Iterable[Mapping[str, object]]

    @property
    def source_revision(self) -> str:
        return f"{self.source.dataset_id}@{self.revision}:{self.source.split}"


DEFAULT_SOURCES: tuple[DatasetSource, ...] = (
    DatasetSource("churn", "Omer1234567/telco-churn-synthetic"),
    DatasetSource("qoe", "fenar/starlink"),
    DatasetSource("ra_fraud", "fenar/revenue_assurance"),
)

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def load_snapshot(source: DatasetSource) -> DatasetSnapshot:
    """Resolve a configured Hub ref once, then load that immutable commit."""
    if _COMMIT_SHA.fullmatch(source.revision):
        resolved_revision = source.revision
    else:
        revision = (
            HfApi()
            .dataset_info(
                repo_id=source.dataset_id,
                revision=source.revision,
            )
            .sha
        )
        if revision is None:
            raise RuntimeError(f"Hugging Face did not resolve a commit SHA for {source.dataset_id}")
        resolved_revision = revision
    dataset = load_dataset(
        path=source.dataset_id,
        split=source.split,
        revision=resolved_revision,
    )
    return DatasetSnapshot(
        source=source,
        revision=resolved_revision,
        rows=cast(Iterable[Mapping[str, object]], dataset),
    )


def normalize_raw_features(row: Mapping[str, object]) -> dict[str, object]:
    """Convert Hub scalar wrappers to JSON-compatible values without dropping fields."""
    return {str(key): _json_value(value) for key, value in row.items()}


def _json_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        # JSONB rejects NaN and infinities; keep the original source value as text.
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        scalar = value.item()
        if scalar is not value:
            return _json_value(cast(object, scalar))
    if hasattr(value, "isoformat"):
        formatted = value.isoformat()
        if isinstance(formatted, str):
            return formatted
    raise TypeError(f"unsupported source value type: {type(value).__name__}")
