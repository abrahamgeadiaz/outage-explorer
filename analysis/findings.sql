-- Queries behind FINDINGS.md. Run from backend/:  uv run outage-sql ../analysis/findings.sql
-- Views: model tables (fact_*, dim_*, metric_fleet_offline_daily, dq_issue) and raw_<dataset>.
-- Each `-- name:` block is one statement; the runner prints its name above the result.

-- name: R1 value reconciliation, 2015-10-01..2015-11-30: national vs sum(facility) vs sum(generator)
WITH f AS (SELECT period, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage, count(*) AS n
           FROM fact_facility_daily GROUP BY 1),
     g AS (SELECT period, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage, count(*) AS n
           FROM fact_generator_daily GROUP BY 1)
SELECT count(*) AS days,
       count(*) FILTER (WHERE f.n IS NULL OR g.n IS NULL) AS days_missing_a_grain,
       count(*) FILTER (WHERE n.capacity_mw <> f.capacity OR f.capacity <> g.capacity) AS days_capacity_gap,
       count(*) FILTER (WHERE n.outage_mw <> f.outage OR f.outage <> g.outage) AS days_outage_gap,
       max(abs(n.capacity_mw - g.capacity)) AS max_capacity_gap_mw,
       max(abs(n.outage_mw - g.outage)) AS max_outage_gap_mw,
       min(f.n) AS min_facilities, max(f.n) AS max_facilities, min(g.n) AS min_units, max(g.n) AS max_units
FROM fact_national_daily n LEFT JOIN f USING (period) LEFT JOIN g USING (period)
WHERE n.period BETWEEN DATE '2015-10-01' AND DATE '2015-11-30';

-- name: R2 value reconciliation, same window: each facility vs the sum of its generators
WITH g AS (SELECT period, facility_id, sum(capacity_mw) AS capacity, sum(outage_mw) AS outage
           FROM fact_generator_daily GROUP BY 1, 2)
SELECT count(*) AS facility_days,
       count(*) FILTER (WHERE f.capacity_mw IS NULL OR g.capacity IS NULL) AS one_side_missing,
       count(*) FILTER (WHERE f.capacity_mw <> g.capacity OR f.outage_mw <> g.outage) AS value_gaps
FROM fact_facility_daily f FULL JOIN g USING (period, facility_id)
WHERE period BETWEEN DATE '2015-10-01' AND DATE '2015-11-30';

-- name: R3 coverage reconciliation, same window: units missing from all grains, per unit
SELECT split_part(unit, '/', 1) AS facility_id, split_part(unit, '/', 2) AS generator_id,
       any_value(d.facility_name) AS facility_name,
       count(*) AS missing_days,
       count(*) FILTER (WHERE isodow(period) IN (6, 7)) AS on_weekends,
       min(period) AS first_missing, max(period) AS last_missing,
       any_value(d.latest_capacity_mw) AS unit_capacity_mw
FROM (SELECT period, unnest(missing_units) AS unit FROM metric_fleet_offline_daily
      WHERE period BETWEEN DATE '2015-10-01' AND DATE '2015-11-30') m
JOIN dim_generator d ON d.facility_id || '/' || d.generator_id = m.unit
GROUP BY ALL ORDER BY facility_id;

-- name: R4 coverage reconciliation, same window: effect on the fleet figure, per day
SELECT period, missing_units, missing_capacity_mw, capacity_mw AS capacity_counted_mw,
       percent_outage_eia, round(offline_pct_min, 2) AS pct_if_online, round(offline_pct_max, 2) AS pct_if_offline,
       round(offline_pct_max - offline_pct_min, 2) AS range_pp
FROM metric_fleet_offline_daily
WHERE period BETWEEN DATE '2015-10-01' AND DATE '2015-11-30' AND NOT is_complete
ORDER BY period;

-- name: R5 control window 2026-09-16..2026-10-16: incomplete days (expect 0)
SELECT count(*) AS days, count(*) FILTER (WHERE NOT is_complete) AS incomplete_days,
       max(abs(offline_pct - percent_outage_eia)) AS max_diff_vs_eia_pp
FROM metric_fleet_offline_daily WHERE period BETWEEN DATE '2026-09-16' AND DATE '2026-10-16';

-- name: R6 whole history: grains, days and how many days are incomplete
SELECT count(*) AS days, count(*) FILTER (WHERE NOT is_complete) AS incomplete_days,
       count(*) FILTER (WHERE units_missing > 0) AS days_with_missing_units,
       count(*) FILTER (WHERE units_zero_capacity > 0) AS days_with_zero_capacity_units,
       max(abs(offline_pct - percent_outage_eia)) AS max_diff_vs_eia_pp,
       max(offline_pct_max - offline_pct_min) AS widest_range_pp
FROM metric_fleet_offline_daily;

-- name: A2.1 units missing from every grain: gaps per unit (dq_issue unit_reporting_gap)
SELECT facility_id, generator_id, any_value(d.facility_name) AS facility_name, count(*) AS gaps,
       sum(days) AS missing_days, max(days) AS longest_gap_days, min(period_start) AS first_gap,
       max(period_end) AS last_gap
FROM dq_issue JOIN dim_generator d USING (facility_id, generator_id)
WHERE check_name = 'unit_reporting_gap' GROUP BY ALL ORDER BY missing_days DESC;

-- name: A2.2 example: River Bend absent on 2015-02-14/15 and the national capacity step
SELECT n.period, n.capacity_mw AS national_capacity_mw,
       n.capacity_mw - lag(n.capacity_mw) OVER (ORDER BY n.period) AS step_mw,
       (SELECT count(*) FROM fact_generator_daily g WHERE g.period = n.period AND g.facility_id = '6462')
           AS river_bend_rows,
       n.percent_outage_eia
FROM fact_national_daily n WHERE n.period BETWEEN DATE '2015-02-12' AND DATE '2015-02-17' ORDER BY 1;

-- name: A2.3 the long gaps start and end on calendar boundaries (month, quarter, half-year)
SELECT facility_id, generator_id, period_start, period_end, days, value_expected AS capacity_mw
FROM dq_issue WHERE check_name = 'unit_reporting_gap' AND days >= 30 ORDER BY period_start;

-- name: A3 Indian Point 2 (2497/2): zero capacity with a reported percentage
SELECT period, capacity_mw, outage_mw, percent_outage_eia, percent_outage
FROM fact_generator_daily
WHERE facility_id = '2497' AND generator_id = '2'
  AND period IN ('2019-12-31', '2020-01-01', '2020-03-17', '2020-03-18', '2020-03-25', '2020-03-31')
ORDER BY period;

-- name: A4 Palisades (1715/1): last row before the gap, return, and today
SELECT period, capacity_mw, outage_mw, percent_outage_eia
FROM fact_generator_daily
WHERE facility_id = '1715' AND period IN ('2022-05-31', '2025-09-09', (SELECT max(period) FROM fact_generator_daily))
ORDER BY period;

-- name: A5 seasonal ratings: units changing capacity on Apr 1 / Oct 1 and the national step
WITH chg AS (
    SELECT a.period, count(*) AS units_changed
    FROM fact_generator_daily a JOIN fact_generator_daily b
      ON a.facility_id = b.facility_id AND a.generator_id = b.generator_id AND b.period = a.period - 1
    WHERE a.capacity_mw <> b.capacity_mw GROUP BY 1
),
-- the step must be computed over every day, before filtering to Apr 1 / Oct 1
steps AS (SELECT period, capacity_mw - lag(capacity_mw) OVER (ORDER BY period) AS national_step_mw
          FROM fact_national_daily)
SELECT period, units_changed, national_step_mw
FROM chg JOIN steps USING (period)
WHERE day(period) = 1 AND month(period) IN (4, 10)
ORDER BY period;

-- name: A6 percent precision by grain: max |EIA - 100*outage/capacity|
SELECT 'national' AS grain, max(abs(percent_outage_eia - percent_outage)) AS max_diff_pp FROM fact_national_daily
UNION ALL SELECT 'facility', max(abs(percent_outage_eia - percent_outage)) FROM fact_facility_daily
UNION ALL SELECT 'generator', max(abs(percent_outage_eia - percent_outage)) FROM fact_generator_daily;

-- name: A7 units first reported late or no longer reported (dq_issue unit_lifecycle)
SELECT facility_id, generator_id, d.facility_name, period_start AS day, detail, value_reported AS capacity_mw
FROM dq_issue JOIN dim_generator d USING (facility_id, generator_id)
WHERE check_name = 'unit_lifecycle' ORDER BY period_start;

-- name: DQ summary of the live build
SELECT check_name, severity, count(*) AS issues, sum(days) AS days_affected
FROM dq_issue GROUP BY ALL ORDER BY severity, check_name;
