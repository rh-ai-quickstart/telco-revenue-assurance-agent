"""Deterministic, source-preserving transformations for the seed datasets."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from typing import SupportsFloat, cast

from data_generator.datasets import DatasetSource, normalize_raw_features

type SourceRow = Mapping[str, object]
type DatabaseRow = dict[str, object]
type RegionClasses = dict[str, tuple[str, str]]

_MONEY_QUANTUM = Decimal("0.000001")
_REGION_CELL_DEGREES = 5.0
_PROFILE_LIMIT = 2048


@dataclass(slots=True)
class CustomerProfile:
    customer_id: str
    bill_total: Decimal
    cycle_count: int
    latest_cycle_start: date
    latest_source_index: int
    latest_source_features: dict[str, object]
    latest_churned: bool

    @property
    def dollar_weight(self) -> Decimal:
        return (self.bill_total / self.cycle_count).quantize(
            _MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )


type CustomerProfiles = dict[str, CustomerProfile]


@dataclass(slots=True)
class RegionSummary:
    region_id: str
    sample_count: int = 0
    signal_loss_total: float = 0.0

    @property
    def mean_signal_loss_db(self) -> float:
        return self.signal_loss_total / self.sample_count


@dataclass(slots=True)
class QoeDatasetProfile:
    regions: dict[str, RegionSummary]
    class_pairs: Counter[tuple[str, str]]
    empirical_values: dict[str, tuple[object, ...]] = field(default_factory=dict)
    source_row_count: int = 0


_CHURN_FIELDS: dict[str, tuple[str, ...]] = {
    "customer_id": ("CustomerID", "customer_id", "customerId"),
    "billing_cycle_start": ("BillingCycleStart", "billing_cycle_start", "cycle_start"),
    "total_bill_amount": ("TotalBillAmount", "total_bill_amount"),
    "has_churned": ("HasChurned", "has_churned", "Churn", "churn"),
}
_QOE_FIELDS: dict[str, tuple[str, ...]] = {
    "latitude": ("latitude", "Latitude", "lat"),
    "longitude": ("longitude", "Longitude", "lon", "lng"),
    "elevation_m": ("elevation_m", "Elevation_m", "elevation", "altitude_m"),
    "visible_satellites": ("visible_satellites", "VisibleSatellites"),
    "serving_satellites": ("serving_satellites", "ServingSatellites"),
    "signal_loss_db": ("signal_loss_db", "SignalLossDB", "signal_loss"),
    "season": ("season", "Season", "season_encoded"),
    "weather": ("weather", "Weather", "weather_encoded"),
}
_RAFM_FIELDS: dict[str, tuple[str, ...]] = {
    "plan_type": ("Plan_Type", "PlanType", "plan_type"),
    "is_fraud": ("Fraud", "is_fraud", "fraud"),
}


def aggregate_churn_profiles(rows: Iterable[SourceRow]) -> CustomerProfiles:
    """Aggregate all billing cycles while retaining the latest source profile."""
    profiles: CustomerProfiles = {}
    for source_index, row in enumerate(rows):
        customer_id = _required_text(row, _CHURN_FIELDS["customer_id"], "CustomerID")
        cycle_start = _required_date(
            row,
            _CHURN_FIELDS["billing_cycle_start"],
            "BillingCycleStart",
        )
        bill_amount = _required_money(
            row,
            _CHURN_FIELDS["total_bill_amount"],
            "TotalBillAmount",
        )
        churned = _required_bool(row, _CHURN_FIELDS["has_churned"], "HasChurned")
        raw_features = normalize_raw_features(row)
        profile = profiles.get(customer_id)
        if profile is None:
            profiles[customer_id] = CustomerProfile(
                customer_id=customer_id,
                bill_total=bill_amount,
                cycle_count=1,
                latest_cycle_start=cycle_start,
                latest_source_index=source_index,
                latest_source_features=raw_features,
                latest_churned=churned,
            )
            continue
        profile.bill_total += bill_amount
        profile.cycle_count += 1
        if (cycle_start, source_index) >= (
            profile.latest_cycle_start,
            profile.latest_source_index,
        ):
            profile.latest_cycle_start = cycle_start
            profile.latest_source_index = source_index
            profile.latest_source_features = raw_features
            profile.latest_churned = churned
    if not profiles:
        raise ValueError("the churn snapshot contains no customer billing rows")
    return profiles


def build_customer_rows(
    profiles: CustomerProfiles,
    region_by_customer: Mapping[str, str],
    plan_by_customer: Mapping[str, str],
    source_dataset_revision: str,
) -> list[DatabaseRow]:
    if set(profiles) != set(region_by_customer) or set(profiles) != set(plan_by_customer):
        raise ValueError("every churn customer needs exactly one region and transaction plan")
    return [
        {
            "customer_id": customer_id,
            "plan_type": plan_by_customer[customer_id],
            "dollar_weight": profile.dollar_weight,
            "region_id": region_by_customer[customer_id],
            "source_dataset_revision": source_dataset_revision,
            "source_features": profile.latest_source_features,
        }
        for customer_id, profile in sorted(profiles.items())
    ]


def iter_billing_cycle_rows(
    rows: Iterable[SourceRow],
    source: DatasetSource,
    source_dataset_revision: str,
) -> Iterable[DatabaseRow]:
    for source_index, row in enumerate(rows):
        customer_id = _required_text(row, _CHURN_FIELDS["customer_id"], "CustomerID")
        yield {
            "billing_cycle_id": stable_source_key(
                "cycle",
                source,
                source_dataset_revision,
                source_index,
                row,
            ),
            "customer_id": customer_id,
            "billing_cycle_start": _required_date(
                row,
                _CHURN_FIELDS["billing_cycle_start"],
                "BillingCycleStart",
            ),
            "total_bill_amount": _required_money(
                row,
                _CHURN_FIELDS["total_bill_amount"],
                "TotalBillAmount",
            ),
            "source_dataset_revision": source_dataset_revision,
            "source_features": normalize_raw_features(row),
        }


def analyze_qoe_rows(rows: Callable[[], Iterable[SourceRow]], seed: int) -> QoeDatasetProfile:
    """Build bounded empirical fallback pools and region summaries in two passes."""
    reservoirs: dict[str, list[object]] = {field_name: [] for field_name in _QOE_FIELDS}
    seen: Counter[str] = Counter()
    for row in rows():
        for field_name, aliases in _QOE_FIELDS.items():
            value = _qoe_value(row, field_name, aliases)
            if value is None:
                continue
            _reservoir_add(reservoirs[field_name], seen[field_name], value, seed, field_name)
            seen[field_name] += 1

    missing_pools = sorted(field_name for field_name, values in reservoirs.items() if not values)
    if missing_pools:
        raise ValueError(f"the QoE snapshot has no usable values for: {', '.join(missing_pools)}")

    regions: dict[str, RegionSummary] = {}
    class_pairs: Counter[tuple[str, str]] = Counter()
    source_row_count = 0
    for source_index, row in enumerate(rows()):
        latitude = _qoe_number(
            row, "latitude", _QOE_FIELDS["latitude"], reservoirs, seed, source_index
        )
        longitude = _qoe_number(
            row,
            "longitude",
            _QOE_FIELDS["longitude"],
            reservoirs,
            seed,
            source_index,
        )
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError(
                f"QoE coordinates are outside valid bounds at source row {source_index}"
            )
        signal_loss = _qoe_number(
            row,
            "signal_loss_db",
            _QOE_FIELDS["signal_loss_db"],
            reservoirs,
            seed,
            source_index,
        )
        region_id = region_id_for(latitude, longitude)
        region = regions.setdefault(region_id, RegionSummary(region_id))
        region.sample_count += 1
        region.signal_loss_total += signal_loss
        season = _qoe_text(
            row,
            "season",
            _QOE_FIELDS["season"],
            reservoirs,
            seed,
            source_index,
        )
        weather = _qoe_text(
            row,
            "weather",
            _QOE_FIELDS["weather"],
            reservoirs,
            seed,
            source_index,
        )
        class_pairs[(season, weather)] += 1
        source_row_count += 1
    if source_row_count == 0:
        raise ValueError("the QoE snapshot contains no samples")
    return QoeDatasetProfile(
        regions=regions,
        class_pairs=class_pairs,
        empirical_values={key: tuple(values) for key, values in reservoirs.items()},
        source_row_count=source_row_count,
    )


def assign_customer_regions(
    profiles: CustomerProfiles,
    qoe_profile: QoeDatasetProfile,
    coupling: float,
    seed: int,
) -> dict[str, str]:
    """Assign high-churn customers to higher signal-loss regions in a tunable share."""
    if not 0 <= coupling <= 1:
        raise ValueError("regional QoE/churn coupling must be between 0 and 1")
    if not profiles or not qoe_profile.regions:
        raise ValueError("regional assignment requires customers and QoE samples")
    region_order = sorted(
        qoe_profile.regions,
        key=lambda region_id: (
            -qoe_profile.regions[region_id].mean_signal_loss_db,
            region_id,
        ),
    )
    if len(region_order) > len(profiles):
        raise ValueError("the QoE snapshot has more geographic regions than churn customers")
    customer_order = sorted(
        profiles,
        key=lambda customer_id: (
            -int(profiles[customer_id].latest_churned),
            _stable_int(seed, f"customer-order:{customer_id}"),
            customer_id,
        ),
    )
    weighted_regions = region_order
    total_weight = sum(qoe_profile.regions[item].sample_count for item in weighted_regions)
    result: dict[str, str] = {}
    for rank, customer_id in enumerate(customer_order):
        coupled = _stable_fraction(seed, f"coupling:{customer_id}") < coupling
        if coupled:
            quantile = (rank + 0.5) / len(customer_order)
            region_id = _weighted_region_at_quantile(
                weighted_regions,
                qoe_profile.regions,
                quantile,
                total_weight,
            )
        else:
            region_id = _weighted_region_for_key(
                weighted_regions,
                qoe_profile.regions,
                seed,
                f"independent:{customer_id}",
                total_weight,
            )
        result[customer_id] = region_id

    _ensure_every_region_has_customer(result, region_order)
    return result


def choose_region_classes(qoe_profile: QoeDatasetProfile, seed: int) -> RegionClasses:
    """Choose an empirical season/weather pair independently of its coordinates row."""
    if not qoe_profile.class_pairs:
        raise ValueError("the QoE snapshot contains no season/weather class pairs")
    pairs = sorted(qoe_profile.class_pairs)
    total_weight = sum(qoe_profile.class_pairs.values())
    result: RegionClasses = {}
    for region_id in sorted(qoe_profile.regions):
        target = _stable_fraction(seed, f"region-classes:{region_id}") * total_weight
        cumulative = 0
        selected = pairs[-1]
        for pair in pairs:
            cumulative += qoe_profile.class_pairs[pair]
            if target < cumulative:
                selected = pair
                break
        result[region_id] = selected
    return result


def customers_by_region(
    region_by_customer: Mapping[str, str],
) -> dict[str, list[str]]:
    members: dict[str, list[str]] = defaultdict(list)
    for customer_id, region_id in sorted(region_by_customer.items()):
        members[region_id].append(customer_id)
    return dict(members)


def iter_qoe_sample_rows(
    rows: Iterable[SourceRow],
    source: DatasetSource,
    source_dataset_revision: str,
    qoe_profile: QoeDatasetProfile,
    customers_by_region_map: Mapping[str, list[str]],
    region_classes: Mapping[str, tuple[str, str]],
    seed: int,
) -> Iterable[DatabaseRow]:
    region_offsets: Counter[str] = Counter()
    for source_index, row in enumerate(rows):
        latitude = _qoe_number(
            row,
            "latitude",
            _QOE_FIELDS["latitude"],
            qoe_profile.empirical_values,
            seed,
            source_index,
        )
        longitude = _qoe_number(
            row,
            "longitude",
            _QOE_FIELDS["longitude"],
            qoe_profile.empirical_values,
            seed,
            source_index,
        )
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError(
                f"QoE coordinates are outside valid bounds at source row {source_index}"
            )
        region_id = region_id_for(latitude, longitude)
        members = customers_by_region_map.get(region_id)
        if not members:
            raise ValueError(f"QoE region {region_id} has no assigned customer")
        customer_id = members[region_offsets[region_id] % len(members)]
        region_offsets[region_id] += 1
        elevation = _qoe_number(
            row,
            "elevation_m",
            _QOE_FIELDS["elevation_m"],
            qoe_profile.empirical_values,
            seed,
            source_index,
        )
        visible_satellites = _qoe_integer(
            row,
            "visible_satellites",
            _QOE_FIELDS["visible_satellites"],
            qoe_profile.empirical_values,
            seed,
            source_index,
        )
        serving_satellites = _qoe_integer(
            row,
            "serving_satellites",
            _QOE_FIELDS["serving_satellites"],
            qoe_profile.empirical_values,
            seed,
            source_index,
        )
        if visible_satellites < 0 or serving_satellites < 0:
            raise ValueError(
                f"QoE satellite counts must be non-negative at source row {source_index}"
            )
        classes = region_classes.get(region_id)
        if classes is None:
            raise ValueError(f"QoE region {region_id} has no empirical season/weather pair")
        yield {
            "qoe_sample_id": stable_source_key(
                "qoe",
                source,
                source_dataset_revision,
                source_index,
                row,
            ),
            "customer_id": customer_id,
            "region_id": region_id,
            "latitude": latitude,
            "longitude": longitude,
            "elevation_m": elevation,
            "visible_satellites": visible_satellites,
            "serving_satellites": serving_satellites,
            "signal_loss_db": _qoe_number(
                row,
                "signal_loss_db",
                _QOE_FIELDS["signal_loss_db"],
                qoe_profile.empirical_values,
                seed,
                source_index,
            ),
            "season": classes[0],
            "weather": classes[1],
            "source_dataset_revision": source_dataset_revision,
            "source_features": normalize_raw_features(row),
        }


def collect_plan_type_counts(rows: Iterable[SourceRow]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = _lookup(row, _RAFM_FIELDS["plan_type"])
        if _is_missing(value):
            continue
        counts[str(value).strip()] += 1
    if not counts:
        raise ValueError("the RAFM snapshot has no usable Plan_Type values")
    return counts


def assign_customer_plan_types(
    customer_ids: Iterable[str],
    plan_type_counts: Mapping[str, int],
    seed: int,
) -> dict[str, str]:
    plan_types = sorted(plan for plan, count in plan_type_counts.items() if count > 0)
    ordered_customers = sorted(
        customer_ids,
        key=lambda customer_id: (_stable_int(seed, f"plan-customer:{customer_id}"), customer_id),
    )
    if not ordered_customers or not plan_types:
        raise ValueError("plan assignment requires customers and RAFM plan types")
    if len(plan_types) > len(ordered_customers):
        raise ValueError("the RAFM snapshot has more plan types than churn customers")
    result = {
        customer_id: plan_types[index]
        for index, customer_id in enumerate(ordered_customers[: len(plan_types)])
    }
    weighted = sorted(plan_type_counts)
    total_weight = sum(plan_type_counts.values())
    for customer_id in ordered_customers[len(plan_types) :]:
        target = _stable_fraction(seed, f"customer-plan:{customer_id}") * total_weight
        cumulative = 0
        for plan_type in weighted:
            cumulative += plan_type_counts[plan_type]
            if target < cumulative:
                result[customer_id] = plan_type
                break
    return result


def iter_transaction_rows(
    rows: Iterable[SourceRow],
    source: DatasetSource,
    source_dataset_revision: str,
    plan_type_counts: Mapping[str, int],
    customer_by_plan: Mapping[str, list[str]],
    seed: int,
) -> Iterable[DatabaseRow]:
    total_plan_weight = sum(plan_type_counts.values())
    for source_index, row in enumerate(rows):
        plan_type = _plan_type_for_row(row, source_index, plan_type_counts, total_plan_weight, seed)
        customer_pool = customer_by_plan.get(plan_type)
        if not customer_pool:
            raise ValueError(f"no customer has RAFM plan type {plan_type!r}")
        customer_index = _stable_int(seed, f"transaction-customer:{source_index}:{plan_type}")
        customer_id = customer_pool[customer_index % len(customer_pool)]
        yield {
            "transaction_id": stable_source_key(
                "transaction",
                source,
                source_dataset_revision,
                source_index,
                row,
            ),
            "customer_id": customer_id,
            "plan_type": plan_type,
            "is_fraud": _required_bool(row, _RAFM_FIELDS["is_fraud"], "Fraud"),
            "source_dataset_revision": source_dataset_revision,
            "source_features": normalize_raw_features(row),
        }


def stable_source_key(
    prefix: str,
    source: DatasetSource,
    source_dataset_revision: str,
    source_index: int,
    row: SourceRow,
) -> str:
    canonical_row = json.dumps(
        normalize_raw_features(row),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    identity = f"{source_dataset_revision}:{source.split}:{source_index}:{canonical_row}"
    return f"{prefix}-{sha256(identity.encode('utf-8')).hexdigest()}"


def region_id_for(latitude: float, longitude: float) -> str:
    lat_bin = min(35, max(0, math.floor((latitude + 90.0) / _REGION_CELL_DEGREES)))
    lon_bin = min(71, max(0, math.floor((longitude + 180.0) / _REGION_CELL_DEGREES)))
    return f"region-{lat_bin:02d}-{lon_bin:02d}"


def _ensure_every_region_has_customer(
    assignment: dict[str, str],
    region_order: list[str],
) -> None:
    assigned_counts = Counter(assignment.values())
    missing = [region_id for region_id in region_order if assigned_counts[region_id] == 0]
    for missing_region in missing:
        donors = [region_id for region_id, count in assigned_counts.items() if count > 1]
        if not donors:
            raise ValueError("not enough customers to cover each QoE region")
        donor_region = max(donors, key=lambda region_id: (assigned_counts[region_id], region_id))
        donor = next(
            customer_id for customer_id, region in assignment.items() if region == donor_region
        )
        assignment[donor] = missing_region
        assigned_counts[donor_region] -= 1
        assigned_counts[missing_region] += 1


def _weighted_region_at_quantile(
    region_ids: list[str],
    regions: Mapping[str, RegionSummary],
    quantile: float,
    total_weight: int,
) -> str:
    target = min(total_weight - 1, int(quantile * total_weight))
    cumulative = 0
    for region_id in region_ids:
        cumulative += regions[region_id].sample_count
        if target < cumulative:
            return region_id
    return region_ids[-1]


def _weighted_region_for_key(
    region_ids: list[str],
    regions: Mapping[str, RegionSummary],
    seed: int,
    key: str,
    total_weight: int,
) -> str:
    target = min(total_weight - 1, int(_stable_fraction(seed, key) * total_weight))
    cumulative = 0
    for region_id in region_ids:
        cumulative += regions[region_id].sample_count
        if target < cumulative:
            return region_id
    return region_ids[-1]


def _qoe_value(row: SourceRow, field_name: str, aliases: tuple[str, ...]) -> object | None:
    value = _lookup(row, aliases)
    if _is_missing(value):
        return None
    try:
        if field_name in {"latitude", "longitude", "elevation_m", "signal_loss_db"}:
            number = float(cast(SupportsFloat | str, value))
            return number if math.isfinite(number) else None
        if field_name in {"visible_satellites", "serving_satellites"}:
            number = float(cast(SupportsFloat | str, value))
            if not math.isfinite(number) or not number.is_integer():
                return None
            return int(number)
        return str(value).strip() or None
    except (TypeError, ValueError, OverflowError):
        return None


def _reservoir_add(
    reservoir: list[object],
    seen_count: int,
    value: object,
    seed: int,
    field_name: str,
) -> None:
    if len(reservoir) < _PROFILE_LIMIT:
        reservoir.append(value)
        return
    slot = _stable_int(seed, f"reservoir:{field_name}:{seen_count}") % (seen_count + 1)
    if slot < _PROFILE_LIMIT:
        reservoir[slot] = value


def _qoe_number(
    row: SourceRow,
    field_name: str,
    aliases: tuple[str, ...],
    pools: Mapping[str, tuple[object, ...]] | Mapping[str, list[object]],
    seed: int,
    source_index: int,
) -> float:
    value = _qoe_value(row, field_name, aliases)
    if value is None:
        value = _empirical_value(pools, field_name, seed, source_index)
    return float(cast(SupportsFloat | str, value))


def _qoe_integer(
    row: SourceRow,
    field_name: str,
    aliases: tuple[str, ...],
    pools: Mapping[str, tuple[object, ...]],
    seed: int,
    source_index: int,
) -> int:
    return int(_qoe_number(row, field_name, aliases, pools, seed, source_index))


def _qoe_text(
    row: SourceRow,
    field_name: str,
    aliases: tuple[str, ...],
    pools: Mapping[str, tuple[object, ...]] | Mapping[str, list[object]],
    seed: int,
    source_index: int,
) -> str:
    value = _qoe_value(row, field_name, aliases)
    if value is None:
        value = _empirical_value(pools, field_name, seed, source_index)
    return str(value)


def _empirical_value(
    pools: Mapping[str, tuple[object, ...]] | Mapping[str, list[object]],
    field_name: str,
    seed: int,
    source_index: int,
) -> object:
    candidates = pools.get(field_name)
    if not candidates:
        raise ValueError(f"no empirical fallback is available for QoE field {field_name!r}")
    index = _stable_int(seed, f"impute:{field_name}:{source_index}") % len(candidates)
    return candidates[index]


def _plan_type_for_row(
    row: SourceRow,
    source_index: int,
    plan_type_counts: Mapping[str, int],
    total_weight: int,
    seed: int,
) -> str:
    value = _lookup(row, _RAFM_FIELDS["plan_type"])
    if not _is_missing(value):
        return str(value).strip()
    target = _stable_int(seed, f"impute-plan:{source_index}") % total_weight
    cumulative = 0
    for plan_type in sorted(plan_type_counts):
        cumulative += plan_type_counts[plan_type]
        if target < cumulative:
            return plan_type
    return sorted(plan_type_counts)[-1]


def _required_text(row: SourceRow, aliases: tuple[str, ...], field_name: str) -> str:
    value = _lookup(row, aliases)
    if _is_missing(value):
        raise ValueError(f"source row is missing required field {field_name}")
    result = str(value).strip()
    if not result:
        raise ValueError(f"source row has an empty required field {field_name}")
    return result


def _required_date(row: SourceRow, aliases: tuple[str, ...], field_name: str) -> date:
    value = _lookup(row, aliases)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _is_missing(value):
        raise ValueError(f"source row is missing required field {field_name}")
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        for date_format in ("%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, date_format).date()
            except ValueError:
                continue
    raise ValueError(f"source field {field_name} is not a supported date")


def _required_money(row: SourceRow, aliases: tuple[str, ...], field_name: str) -> Decimal:
    value = _lookup(row, aliases)
    if _is_missing(value):
        raise ValueError(f"source row is missing required field {field_name}")
    try:
        result = Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"source field {field_name} is not a valid amount") from error
    if not result.is_finite() or result < 0 or result >= Decimal("1000000000000"):
        raise ValueError(f"source field {field_name} is outside the database amount range")
    return result


def _required_bool(row: SourceRow, aliases: tuple[str, ...], field_name: str) -> bool:
    value = _lookup(row, aliases)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return bool(int(value))
    normalized = str(value).strip().casefold() if value is not None else ""
    if normalized in {"true", "1", "yes", "y", "churned", "fraud", "positive"}:
        return True
    if normalized in {"false", "0", "no", "n", "not churned", "non-fraud", "negative"}:
        return False
    raise ValueError(f"source field {field_name} is not a supported boolean label")


def _lookup(row: SourceRow, aliases: tuple[str, ...]) -> object | None:
    columns = {_normalize_column(str(name)): value for name, value in row.items()}
    for alias in aliases:
        key = _normalize_column(alias)
        if key in columns:
            return columns[key]
    return None


def _normalize_column(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_missing(value: object | None) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, float):
        return not math.isfinite(value)
    return False


def _stable_int(seed: int, key: str) -> int:
    digest = sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _stable_fraction(seed: int, key: str) -> float:
    digest = sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) / (2**256)
