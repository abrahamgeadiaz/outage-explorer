"""Model table definitions: columns, types, primary keys and descriptions.

This is the single description of the model. The build writes each table with exactly these columns
and types and checks the primary key; the catalog (Part 3) serves the descriptions, so metric
definitions reach users from the same place the code uses.

Types:
- MW values are DECIMAL(12,3): EIA sends at most 3 decimals (checked on the full load), so the cast
  is exact and sums across grains reconcile exactly instead of drifting by float noise (~1e-11).
- `percent_outage_eia` is what EIA sent (rounded to 2 decimals at facility and national level);
  `percent_outage` is recomputed from MW, unrounded, and NULL when capacity is not positive.
- Ids stay VARCHAR, as EIA sends them (DECISIONS.md #1).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    type: str  # DuckDB SQL type
    description: str


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    grain: str
    primary_key: tuple[str, ...]
    columns: tuple[Column, ...]
    partitioned_by_year: bool = False
    foreign_keys: dict[tuple[str, ...], str] = field(default_factory=dict)  # columns -> referenced table

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


MW = "DECIMAL(12,3)"
PCT_EIA = "DECIMAL(6,2)"

_PERIOD = Column("period", "DATE", "Day the values refer to.")
_FACILITY = Column("facility_id", "VARCHAR", "EIA plant id (EIA-860 plant code).")
_GENERATOR = Column("generator_id", "VARCHAR", "EIA unit id, unique only within its facility.")
_NAME = Column("facility_name", "VARCHAR", "Plant name as published by EIA (label, not part of any key).")
_VALUES = (
    Column("capacity_mw", MW, "Net capacity in MW that EIA counts for this day."),
    Column("outage_mw", MW, "Capacity offline in MW (planned or unplanned)."),
    Column(
        "percent_outage",
        "DOUBLE",
        "100 * outage_mw / capacity_mw, recomputed and unrounded; NULL when capacity_mw <= 0.",
    ),
    Column("percent_outage_eia", PCT_EIA, "percentOutage as published by EIA (rounded)."),
)
_LINEAGE = (
    Column("_run_id", "VARCHAR", "Extraction run that last changed this row."),
    Column("_ingested_at", "TIMESTAMP", "When that run stored the row (UTC)."),
)

FACT_NATIONAL = Table(
    "fact_national_daily",
    "U.S. fleet totals per day, as published by EIA (us-nuclear-outages).",
    "one row per day",
    ("period",),
    (_PERIOD, *_VALUES, *_LINEAGE),
    partitioned_by_year=True,
)
FACT_FACILITY = Table(
    "fact_facility_daily",
    "Plant totals per day, as published by EIA (facility-nuclear-outages).",
    "one row per day and facility",
    ("period", "facility_id"),
    (_PERIOD, _FACILITY, _NAME, *_VALUES, *_LINEAGE),
    partitioned_by_year=True,
    foreign_keys={("facility_id",): "dim_facility"},
)
FACT_GENERATOR = Table(
    "fact_generator_daily",
    "Unit values per day, as published by EIA (generator-nuclear-outages). Finest grain.",
    "one row per day, facility and generator",
    ("period", "facility_id", "generator_id"),
    (_PERIOD, _FACILITY, _GENERATOR, _NAME, *_VALUES, *_LINEAGE),
    partitioned_by_year=True,
    foreign_keys={("facility_id", "generator_id"): "dim_generator", ("facility_id",): "dim_facility"},
)

_LIFETIME = (
    Column("first_period", "DATE", "First day with a row in the generator data."),
    Column("last_period", "DATE", "Last day with a row in the generator data."),
)
_STATUS = Column(
    "status",
    "VARCHAR",
    "'active' if reported on the latest day in the data, else 'retired' (EIA has no retirement "
    "field: rows simply stop).",
)

DIM_FACILITY = Table(
    "dim_facility",
    "Nuclear plants seen in the generator data, with their reporting lifetime.",
    "one row per facility",
    ("facility_id",),
    (
        _FACILITY,
        _NAME,
        *_LIFETIME,
        Column("generator_count", "INTEGER", "Units ever reported for this plant."),
        _STATUS,
    ),
)
DIM_GENERATOR = Table(
    "dim_generator",
    "Nuclear units seen in the generator data, with their reporting lifetime and gaps.",
    "one row per facility and generator",
    ("facility_id", "generator_id"),
    (
        _FACILITY,
        _GENERATOR,
        _NAME,
        *_LIFETIME,
        Column("reported_days", "INTEGER", "Days with a row."),
        Column(
            "missing_days",
            "INTEGER",
            "Days between first_period and last_period with no row in any grain (reporting gaps).",
        ),
        Column("latest_capacity_mw", MW, "capacity_mw on last_period."),
        _STATUS,
    ),
    foreign_keys={("facility_id",): "dim_facility"},
)

METRIC_FLEET_OFFLINE = Table(
    "metric_fleet_offline_daily",
    "US-08: share of total fleet capacity offline per day, with how complete that day's data is. "
    "offline_pct uses the units EIA reported that day (so it matches EIA's national figure). Units "
    "missing that day are never filled in: their capacity is reported, and offline_pct_min/max give "
    "the range the true share falls in.",
    "one row per day",
    ("period",),
    (
        _PERIOD,
        Column("capacity_mw", MW, "Σ capacity_mw of the units reported that day with capacity > 0."),
        Column("outage_mw", MW, "Σ outage_mw of those units."),
        Column("offline_pct", "DOUBLE", "100 * outage_mw / capacity_mw: the US-08 metric."),
        Column("percent_outage_eia", PCT_EIA, "EIA's national percentOutage, for comparison (rounded)."),
        Column("units_reported", "INTEGER", "Units with a row and capacity > 0 that day."),
        Column(
            "units_missing",
            "INTEGER",
            "Units inside their reporting lifetime (dim_generator) with no row that day, in any grain.",
        ),
        Column("missing_units", "VARCHAR[]", "Those units, as 'facility_id/generator_id'."),
        Column("missing_capacity_mw", MW, "Σ last reported capacity_mw of the missing units."),
        Column(
            "units_zero_capacity", "INTEGER", "Units reported with capacity_mw <= 0 (left out of the share)."
        ),
        Column(
            "offline_pct_min",
            "DOUBLE",
            "Share if every missing unit was fully online: outage / (capacity + missing capacity).",
        ),
        Column(
            "offline_pct_max",
            "DOUBLE",
            "Share if every missing unit was fully offline: (outage + missing) / (capacity + missing).",
        ),
        Column("is_complete", "BOOLEAN", "No missing and no zero-capacity units: offline_pct is exact."),
    ),
    foreign_keys={("period",): "fact_national_daily"},
)

DQ_ISSUE = Table(
    "dq_issue",
    "Data quality findings from the checks run on every build (model/quality.py). Runs of "
    "consecutive days are one row. The product reports these; it does not correct EIA's values.",
    "one row per finding",
    ("issue_id",),
    (
        Column("issue_id", "INTEGER", "Row number within this build (not stable across builds)."),
        Column("check_name", "VARCHAR", "Which check found it, e.g. 'unit_reporting_gap'."),
        Column("severity", "VARCHAR", "'error' (values contradict), 'warning' (incomplete) or 'info'."),
        Column("dataset", "VARCHAR", "Grain the finding is about: national, facility or generator."),
        Column("facility_id", "VARCHAR", "Facility, when the finding is about one."),
        Column("generator_id", "VARCHAR", "Generator, when the finding is about one."),
        Column("period_start", "DATE", "First day affected."),
        Column("period_end", "DATE", "Last day affected."),
        Column("days", "INTEGER", "Days affected (period_end - period_start + 1)."),
        Column("value_reported", "DOUBLE", "The value EIA reported (meaning depends on the check)."),
        Column("value_expected", "DOUBLE", "The value it should match, or the MW at stake."),
        Column("detail", "VARCHAR", "Human-readable description."),
    ),
)

TABLES: dict[str, Table] = {
    t.name: t
    for t in (
        DIM_FACILITY,
        DIM_GENERATOR,
        FACT_NATIONAL,
        FACT_FACILITY,
        FACT_GENERATOR,
        METRIC_FLEET_OFFLINE,
        DQ_ISSUE,
    )
}
