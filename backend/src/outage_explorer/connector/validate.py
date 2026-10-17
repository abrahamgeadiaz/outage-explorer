"""Turn raw EIA JSON rows into a typed Arrow table, rejecting rows we cannot store safely.

Required (row rejected if missing/invalid):
- period and every natural-key column: without them a row has no identity, so re-runs could not
  replace it and it would duplicate.
- capacity and outage: every metric in the product is built from these two numbers.
- units, when present, must be megawatts/percent: a silent unit change would corrupt every sum.
- period must fall inside the requested window: rows outside it would escape the window replace.

Optional: percentOutage (derivable from outage/capacity, recomputed in the model) and facilityName.

Semantic oddities (negative values, outage > capacity, duplicate keys) are *kept* and counted:
they are findings about the source, handled in the data model, not reasons to drop data here.
"""

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import pyarrow as pa

from outage_explorer.connector.routes import Route

EXPECTED_UNITS = {
    "capacity-units": "megawatts",
    "outage-units": "megawatts",
    "percentOutage-units": "percent",
}
KNOWN_FIELDS = {
    "period",
    "facility",
    "facilityName",
    "generator",
    "capacity",
    "outage",
    "percentOutage",
} | set(EXPECTED_UNITS)
MAX_REJECTED_EXAMPLES = 20

# EIA camelCase -> our snake_case column names
COLUMN_NAMES = {"facilityName": "facility_name", "percentOutage": "percent_outage"}


def schema_for(route: Route) -> pa.Schema:
    fields = [pa.field("period", pa.date32(), nullable=False)]
    fields += [pa.field(c, pa.string(), nullable=False) for c in route.key_columns if c != "period"]
    fields += [pa.field(COLUMN_NAMES.get(c, c), pa.string()) for c in route.label_columns]
    fields += [
        pa.field("capacity", pa.float64(), nullable=False),
        pa.field("outage", pa.float64(), nullable=False),
        pa.field("percent_outage", pa.float64()),
    ]
    return pa.schema(fields)


@dataclass
class ValidationResult:
    table: pa.Table
    rows_in: int
    rejected: Counter = field(default_factory=Counter)  # reason -> count
    warnings: Counter = field(default_factory=Counter)  # reason -> count (rows kept)
    rejected_examples: list[dict] = field(default_factory=list)
    unknown_fields: set[str] = field(default_factory=set)

    @property
    def rows_rejected(self) -> int:
        return sum(self.rejected.values())


class _Reject(Exception):
    pass


def validate_rows(route: Route, rows: list[dict], start: date, end: date) -> ValidationResult:
    result = ValidationResult(table=schema_for(route).empty_table(), rows_in=len(rows))
    by_key: dict[tuple, dict] = {}

    for raw in rows:
        result.unknown_fields |= set(raw) - KNOWN_FIELDS
        try:
            clean = _validate_row(route, raw, start, end, result.warnings)
        except _Reject as exc:
            reason = str(exc)
            result.rejected[reason] += 1
            if len(result.rejected_examples) < MAX_REJECTED_EXAMPLES:
                result.rejected_examples.append({"reason": reason, "row": raw})
            continue
        key = tuple(clean[c] for c in route.key_columns)
        if key in by_key:
            result.warnings["duplicate_key"] += 1  # last one wins, matching the merge rule in storage
        by_key[key] = clean

    if by_key:
        result.table = pa.Table.from_pylist(list(by_key.values()), schema=schema_for(route))
    return result


def _validate_row(route: Route, raw: dict, start: date, end: date, warnings: Counter) -> dict:
    out: dict = {}

    try:
        period = date.fromisoformat(str(raw.get("period") or ""))
    except ValueError:
        raise _Reject("invalid_period") from None
    if not start <= period <= end:
        raise _Reject("period_out_of_window")
    out["period"] = period

    for col in route.key_columns:
        if col == "period":
            continue
        value = str(raw.get(col) or "").strip()
        if not value:
            raise _Reject(f"missing_{col}")
        out[col] = value

    for col in route.label_columns:
        value = raw.get(col)
        out[COLUMN_NAMES.get(col, col)] = str(value).strip() if value not in (None, "") else None

    for unit_field, expected in EXPECTED_UNITS.items():
        if unit_field in raw and raw[unit_field] != expected:
            raise _Reject(f"unexpected_{unit_field}")

    out["capacity"] = _number(raw.get("capacity"), "capacity", required=True)
    out["outage"] = _number(raw.get("outage"), "outage", required=True)
    try:
        out["percent_outage"] = _number(raw.get("percentOutage"), "percentOutage", required=False)
    except _Reject:
        warnings["invalid_percentOutage"] += 1
        out["percent_outage"] = None

    if out["capacity"] < 0:
        warnings["negative_capacity"] += 1
    if out["outage"] < 0:
        warnings["negative_outage"] += 1
    if out["outage"] > out["capacity"]:
        warnings["outage_exceeds_capacity"] += 1
    return out


def _number(value, name: str, *, required: bool) -> float | None:
    if value is None or value == "":
        if required:
            raise _Reject(f"missing_{name}")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _Reject(f"invalid_{name}") from None
    if not math.isfinite(number):
        raise _Reject(f"invalid_{name}")
    return number
