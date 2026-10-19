"""Data quality checks, run on every model build; their findings become the `dq_issue` table.

Each check is one SELECT over the model tables (and, for duplicates, the raw views), returning rows
with the dq_issue columns. Runs of consecutive days are collapsed into one issue (period_start..end),
so a unit missing for 1,196 days is one row, not 1,196.

The checks encode the anomalies found in the real data (FINDINGS.md), so a future load that repeats
them, or breaks something that holds today (the grains adding up), is reported automatically:

- errors: values that contradict each other or the definition (grains don't add up, a percentage
  without capacity, outage above capacity). The product shows them; it does not correct EIA.
- warnings: data that is valid but incomplete (a unit missing from every grain, duplicates dropped).
- info: lifecycle events that change the fleet (a unit first reported or no longer reported).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    sql: str  # SELECT producing the dq_issue columns except issue_id and check_name


# Every check selects these columns, in this order (UNION ALL BY NAME makes the order irrelevant,
# but a missing column would be an error, which is what we want).
_ISSUE = """
    {severity} AS severity, {dataset} AS dataset, {facility} AS facility_id, {generator} AS generator_id,
    ({start})::DATE AS period_start, ({end})::DATE AS period_end,
    (({end})::DATE - ({start})::DATE + 1)::INTEGER AS days,
    ({reported})::DOUBLE AS value_reported, ({expected})::DOUBLE AS value_expected, {detail} AS detail
"""


def _cols(**kw: str) -> str:
    return _ISSUE.format(**kw)


GRAIN_MISMATCH_NATIONAL = Check(
    "grain_mismatch_national",
    "National capacity or outage differs from the sum of the facility or generator rows that day, "
    "or a grain has no rows for a day the others have.",
    """
    WITH f AS (SELECT period, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage
               FROM fact_facility_daily GROUP BY 1),
         g AS (SELECT period, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage
               FROM fact_generator_daily GROUP BY 1),
         cmp AS (
            SELECT period, n.capacity_mw AS nat_capacity, n.outage_mw AS nat_outage,
                   f.capacity AS fac_capacity, f.outage AS fac_outage,
                   g.capacity AS gen_capacity, g.outage AS gen_outage
            FROM fact_national_daily n FULL JOIN f USING (period) FULL JOIN g USING (period)
         ),
         pairs AS (
            SELECT period, 'capacity_mw' AS measure, 'facility' AS other, nat_capacity AS nat,
                   fac_capacity AS val FROM cmp
            UNION ALL SELECT period, 'capacity_mw', 'generator', nat_capacity, gen_capacity FROM cmp
            UNION ALL SELECT period, 'outage_mw', 'facility', nat_outage, fac_outage FROM cmp
            UNION ALL SELECT period, 'outage_mw', 'generator', nat_outage, gen_outage FROM cmp
         )
    SELECT """
    + _cols(
        severity="'error'",
        dataset="'national'",
        facility="NULL",
        generator="NULL",
        start="period",
        end="period",
        reported="nat",
        expected="val",
        detail="""CASE WHEN nat IS NULL THEN 'no national row; ' || other || ' rows exist'
                       WHEN val IS NULL THEN 'national row but no ' || other || ' rows'
                       ELSE 'national ' || measure || ' vs sum of ' || other || ' rows: gap '
                            || (nat - val)::VARCHAR || ' MW' END""",
    )
    + " FROM pairs WHERE nat IS DISTINCT FROM val",
)

GRAIN_MISMATCH_FACILITY = Check(
    "grain_mismatch_facility",
    "A facility row differs from the sum of its generators that day, a facility row has no "
    "generators, or generators have no facility row (DECISIONS.md #3).",
    """
    WITH g AS (SELECT period, facility_id, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage
               FROM fact_generator_daily GROUP BY 1, 2),
         cmp AS (SELECT period, facility_id, f.capacity_mw AS fac_capacity, f.outage_mw AS fac_outage,
                        g.capacity AS gen_capacity, g.outage AS gen_outage
                 FROM fact_facility_daily f FULL JOIN g USING (period, facility_id)),
         pairs AS (
            SELECT period, facility_id, 'capacity_mw' AS measure, fac_capacity AS fac,
                   gen_capacity AS gen FROM cmp
            UNION ALL SELECT period, facility_id, 'outage_mw', fac_outage, gen_outage FROM cmp
         )
    SELECT """
    + _cols(
        severity="'error'",
        dataset="'facility'",
        facility="facility_id",
        generator="NULL",
        start="period",
        end="period",
        reported="fac",
        expected="gen",
        detail="""CASE WHEN fac IS NULL THEN 'generators reported but no facility row'
                       WHEN gen IS NULL THEN 'facility row but no generator rows'
                       ELSE 'facility ' || measure || ' vs sum of its generators: gap '
                            || (fac - gen)::VARCHAR || ' MW' END""",
    )
    + " FROM pairs WHERE fac IS DISTINCT FROM gen",
)

UNIT_REPORTING_GAP = Check(
    "unit_reporting_gap",
    "A unit has no row in any grain for days inside its reporting lifetime, so EIA's capacity for "
    "those days leaves it out. value_expected is its last reported capacity.",
    """
    WITH steps AS (
        SELECT facility_id, generator_id, period,
               lag(period) OVER w AS prev_period, lag(capacity_mw) OVER w AS prev_capacity
        FROM fact_generator_daily
        WINDOW w AS (PARTITION BY facility_id, generator_id ORDER BY period)
    )
    SELECT """
    + _cols(
        severity="'warning'",
        dataset="'generator'",
        facility="facility_id",
        generator="generator_id",
        start="prev_period + 1",
        end="period - 1",
        reported="0",
        expected="prev_capacity",
        detail="'no row in any grain; ' || prev_capacity::VARCHAR || ' MW missing from the fleet total'",
    )
    + " FROM steps WHERE period - prev_period > 1",
)

NON_POSITIVE_CAPACITY = Check(
    "non_positive_capacity",
    "A unit is reported with capacity <= 0. It is an error when EIA still reports an outage or a "
    "percentage for those days (impossible without capacity); percent_outage is NULL there.",
    """
    WITH flagged AS (
        SELECT facility_id, generator_id, period, outage_mw, percent_outage_eia,
               period - (row_number() OVER (PARTITION BY facility_id, generator_id ORDER BY period))::INTEGER
                   AS island
        FROM fact_generator_daily WHERE capacity_mw <= 0
    ),
    runs AS (
        SELECT facility_id, generator_id, min(period) AS first_day, max(period) AS last_day,
               max(outage_mw) AS max_outage, max(percent_outage_eia) AS max_pct,
               min(period) FILTER (WHERE percent_outage_eia > 0 OR outage_mw > 0) AS first_contradiction
        FROM flagged GROUP BY facility_id, generator_id, island
    )
    SELECT """
    + _cols(
        severity="CASE WHEN first_contradiction IS NULL THEN 'warning' ELSE 'error' END",
        dataset="'generator'",
        facility="facility_id",
        generator="generator_id",
        start="first_day",
        end="last_day",
        reported="max_pct",
        expected="0",
        detail="""'capacity 0 MW' || CASE WHEN first_contradiction IS NULL THEN ''
                  ELSE '; EIA still reports up to ' || max_pct::VARCHAR || '% outage from '
                       || first_contradiction::VARCHAR END""",
    )
    + " FROM runs",
)

_PERCENT_SOURCES = (
    ("national", "fact_national_daily", "NULL", "NULL"),
    ("facility", "fact_facility_daily", "facility_id", "NULL"),
    ("generator", "fact_generator_daily", "facility_id", "generator_id"),
)

PERCENT_MISMATCH = Check(
    "percent_mismatch",
    "EIA's percentOutage differs from 100 * outage / capacity by more than its 2-decimal rounding.",
    " UNION ALL ".join(
        "SELECT "
        + _cols(
            severity="'error'",
            dataset=f"'{dataset}'",
            facility=fac,
            generator=gen,
            start="period",
            end="period",
            reported="percent_outage_eia",
            expected="percent_outage",
            detail="'EIA percentOutage ' || percent_outage_eia::VARCHAR || ' vs computed '"
            " || round(percent_outage, 3)::VARCHAR",
        )
        + f" FROM {table} WHERE abs(percent_outage_eia - percent_outage) > 0.005 + 1e-9"
        for dataset, table, fac, gen in _PERCENT_SOURCES
    ),
)

VALUE_OUT_OF_RANGE = Check(
    "value_out_of_range",
    "Negative capacity or outage, or outage greater than capacity.",
    " UNION ALL ".join(
        "SELECT "
        + _cols(
            severity="'error'",
            dataset=f"'{dataset}'",
            facility=fac,
            generator=gen,
            start="period",
            end="period",
            reported="outage_mw",
            expected="capacity_mw",
            detail="'capacity ' || capacity_mw::VARCHAR || ' MW, outage ' || outage_mw::VARCHAR || ' MW'",
        )
        + f" FROM {table} WHERE capacity_mw < 0 OR outage_mw < 0 OR outage_mw > capacity_mw"
        for dataset, table, fac, gen in _PERCENT_SOURCES
    ),
)

UNIT_LIFECYCLE = Check(
    "unit_lifecycle",
    "A unit is first reported after the data starts, or no longer reported (EIA has no retirement "
    "field). Fleet capacity steps on these days are real changes, not errors.",
    """
    WITH bounds AS (SELECT min(first_period) AS data_start, max(last_period) AS data_end FROM dim_generator),
         events AS (
            SELECT facility_id, generator_id, first_period AS day, latest_capacity_mw AS capacity,
                   'first reported' AS what
            FROM dim_generator, bounds WHERE first_period > data_start
            UNION ALL
            SELECT facility_id, generator_id, last_period, latest_capacity_mw, 'last reported'
            FROM dim_generator, bounds WHERE last_period < data_end
         )
    SELECT """
    + _cols(
        severity="'info'",
        dataset="'generator'",
        facility="facility_id",
        generator="generator_id",
        start="day",
        end="day",
        reported="capacity",
        expected="NULL",
        detail="'unit ' || what || ' on ' || day::VARCHAR",
    )
    + " FROM events",
)

DUPLICATE_KEYS = Check(
    "duplicate_keys",
    "Raw rows sharing a natural key; the build kept the most recently ingested one.",
    " UNION ALL ".join(
        "SELECT "
        + _cols(
            severity="'warning'",
            dataset=f"'{dataset}'",
            facility="NULL",
            generator="NULL",
            start="min(period)",
            end="max(period)",
            reported=f"(SELECT count(*) FROM raw_{dataset})",
            expected=f"(SELECT count(*) FROM src_{dataset})",
            detail=f"((SELECT count(*) FROM raw_{dataset}) - (SELECT count(*) FROM src_{dataset}))::VARCHAR"
            " || ' duplicate rows dropped'",
        )
        + f" FROM raw_{dataset}"
        f" HAVING (SELECT count(*) FROM raw_{dataset}) > (SELECT count(*) FROM src_{dataset})"
        for dataset in ("national", "facility", "generator")
    ),
)

CHECKS: tuple[Check, ...] = (
    GRAIN_MISMATCH_NATIONAL,
    GRAIN_MISMATCH_FACILITY,
    UNIT_REPORTING_GAP,
    NON_POSITIVE_CAPACITY,
    PERCENT_MISMATCH,
    VALUE_OUT_OF_RANGE,
    UNIT_LIFECYCLE,
    DUPLICATE_KEYS,
)


def dq_issue_sql() -> str:
    """One SELECT combining every check, with a stable issue_id."""
    parts = [f"SELECT '{c.name}' AS check_name, * FROM ({c.sql})" for c in CHECKS]
    return f"""
        SELECT row_number() OVER (ORDER BY check_name, dataset, facility_id, generator_id, period_start)
                   AS issue_id, *
        FROM ({" UNION ALL BY NAME ".join(parts)})
    """
