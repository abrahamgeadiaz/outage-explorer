# Findings: where the three EIA grains disagree, and why

Based on the full extraction of 2026-10-17 (2007-01-01 → 2026-10-16): 7,229 national, 437,349 facility
and 713,327 generator rows. Every number below comes from a query you can re-run:

```bash
cd backend
uv run outage-model                              # build the model (~25 s)
uv run outage-sql ../analysis/findings.sql       # every query below, labelled R1..R6, A1..A7
uv run python ../analysis/check_facility_total.py 2026-10-01   # A1 (calls the API, needs EIA_API_KEY)
```

The same checks run automatically on every model build and write to the `dq_issue` table
([`model/quality.py`](backend/src/outage_explorer/model/quality.py)). Model and ER diagram:
[docs/model.md](docs/model.md).

## Summary

- **The values always agree.** On every one of the 7,229 days, the national row equals the sum of
  the facility rows, which equals the sum of the generator rows. Each facility row also equals the
  sum of its units. This holds for capacity and outage, exactly (`0.000` MW once stored as DECIMAL).
  EIA builds the upper grains by adding up the units it has that day.
- **What disagrees is coverage.** Some days a unit is missing from **all three** grains at once. The
  sums still agree, but the fleet total quietly loses ~1 GW. This happens on 1,801 days (99 gaps,
  5 units), and it is the gap that matters for US-08.
- Seven anomalies are documented below (A1–A7). Each one has a concrete example and says how the
  product handles it.

## Reconciliation, 2015-10-01 → 2015-11-30 (61 days)

**Why this window:** it is the stretch where two different units drop out repeatedly: River Bend, on
weekends, and Watts Bar 2, on weekdays in its first weeks of reporting. So it shows the coverage problem
from both sides. A recent window is used as a control.

### Values: no gaps (R1, R2)

| Comparison | Days / rows compared | Mismatches | Largest gap |
|---|---|---|---|
| national vs Σ facility vs Σ generator (capacity, outage) | 61 days | 0 | 0.000 MW |
| facility vs Σ its generators (capacity, outage) | 3,760 facility-days | 0 (and no row missing on either side) | 0.000 MW |

Between 61 and 62 facilities and 98 to 99 units were reported per day.

### Coverage: 44 unit-days missing from all grains (R3, R4)

| Unit | Missing days in window | On weekends | First → last | Capacity |
|---|---|---|---|---|
| River Bend Station (6462/1) | 22 | 18 | 2015-10-03 → 2015-11-29 | 967 MW |
| Watts Bar 2 (7722/2) | 22 | 0 | 2015-10-28 → 2015-11-30 | 1,164 MW |

On those 44 days the national capacity drops by the missing unit's rating, and EIA's percentage is
computed over a smaller fleet. Examples from R4:

| Day | Missing | Capacity counted | EIA % offline | If the unit was online | If it was offline | Range |
|---|---|---|---|---|---|---|
| 2015-10-03 | River Bend, 967 MW | 100,034.4 MW | 14.53 | 14.39 | 15.35 | 0.96 pp |
| 2015-10-28 | Watts Bar 2, 1,164 MW | 101,001.4 MW | 20.89 | 20.65 | 21.79 | 1.14 pp |
| 2015-11-08 | River Bend, 967 MW | 101,198.4 MW | 24.16 | 23.93 | 24.88 | 0.95 pp |

Only 17 of the 61 days are complete, all before Watts Bar 2 is first reported (2015-10-27), at
101,001.4 MW. **From 2015-10-27 to 2015-11-30, not one day is complete:** every day, either River
Bend or Watts Bar 2 is missing. The counted capacity alternates between 101,198.4 and 101,001.4 MW:
the full fleet (102,165.4 MW) minus one unit. For 35 days in a row, EIA's national figure never
describes the whole fleet.

**Why they don't match:** EIA's facility and national rows are sums over the units it has that day.
When a unit has no row, it is left out of all three grains together. The grains stay consistent with
each other, but they all describe a smaller fleet than the one that exists. The data alone can't say
why a unit is missing:

- River Bend's gaps are mostly weekends (18 of 22 here).
- Watts Bar 2 was only first reported on 2015-10-27, and its gaps are runs of weekdays.

Neither looks like a plant event: on the days around each gap, both units report normally.

### Control window and whole history (R5, R6)

| Window | Days | Days with a missing or zero-capacity unit | Max \|our % − EIA %\| |
|---|---|---|---|
| 2026-09-16 → 2026-10-16 | 31 | 0 | 0.0048 pp (EIA's rounding) |
| 2007-01-01 → 2026-10-16 | 7,229 | 1,892 (1,801 missing units, 91 zero capacity) | 0.005 pp |

The widest range on any day is 1.95 percentage points (two units missing at once).

**How the product handles it:** the US-08 metric (`metric_fleet_offline_daily`) reproduces EIA's
figure but never presents an incomplete day as complete. Each day lists `missing_units`,
`missing_capacity_mw`, `offline_pct_min`/`offline_pct_max` and `is_complete`. Units are not filled
in, because we can't know whether a missing unit was running (DECISIONS.md #17).

## Anomalies

### A1. The facility route reports a wrong `total`

- **Example:** a request for 2026-10-01 on `facility-nuclear-outages` returns `total = 95` and 55 rows.
  95 is that day's *generator* count; the national and generator routes report correctly (1/1, 95/95).
  Over the full history, the facility route reports 713,327 (the generator row count) while it
  returns 437,349 rows.
- **Why it matters:** a client that pages until it reaches `total` would request hundreds of empty
  pages, or report a correct load as incomplete.
- **Handling:** the connector ignores `total` and stops on the first short page (DECISIONS.md #8).
  Re-run: `analysis/check_facility_total.py`.

### A2. Units vanish from all three grains for days or years

| Unit | Gaps | Missing days | Longest gap | Period |
|---|---|---|---|---|
| Palisades (1715/1) | 1 | 1,196 | 1,196 | 2022-06-01 → 2025-09-08 |
| River Bend (6462/1) | 84 | 564 | 120 | 2013-01-01 → 2019-01-23 |
| Vogtle 3 (649/3) | 1 | 181 | 181 | 2023-01-01 → 2023-06-30 |
| Vogtle 4 (649/4) | 1 | 91 | 91 | 2024-01-01 → 2024-03-31 |
| Watts Bar 2 (7722/2) | 12 | 41 | 5 | 2015-10-28 → 2016-02-29 |

- **Example:** River Bend has no row on 2015-02-14 and 02-15. National capacity drops from
  100,859 to 99,892 MW (−967 MW) and comes back on 02-16. EIA's percentage for 02-14 is 4.89%; with
  River Bend counted it would be between 4.84% and 5.80% (A2.2).
- **Observation:** the long gaps start and end on calendar boundaries: 2013-01-01→04-30,
  2016-03-01→05-31, 2023-01-01→06-30 and 2024-01-01→03-31 (A2.3). That points to how the data is
  processed rather than to events at the plants. This is a hypothesis; the data can't confirm it.
- **Handling:**
  - `unit_reporting_gap` warning in `dq_issue`, one row per gap, with the MW at stake;
  - `dim_generator.missing_days`;
  - the US-08 coverage columns and range.

### A3. Indian Point 2: zero capacity, yet a percentage outage

- **Example (2497/2):**
  - capacity is 1,025.9 MW on 2019-12-31 and **0 MW from 2020-01-01 to 2020-03-31** (91 days),
    its last row;
  - from 2020-03-18, `percentOutage` rises day by day from 1 to 11 while capacity and outage are
    both 0 (A3).

  A percentage of zero capacity is undefined, so EIA's number contradicts its own MW values.
- **Handling:**
  - `percent_outage` (recomputed) is NULL, and EIA's value stays visible in `percent_outage_eia`;
  - the unit is left out of the US-08 share and counted in `units_zero_capacity`, so those 91 days
    are not `is_complete`;
  - `non_positive_capacity` reports it as an **error**, the only one in the data.

### A4. A unit comes back after three years, at 100% outage

- **Example (Palisades 1715/1):**
  - last row before the gap: 2022-05-31, 768.5 MW, 100% outage;
  - back on 2025-09-09 with the same values;
  - on 2026-10-16 still 100% outage, at 815.6 MW (A4).

  Since its return it adds its whole rating to the national outage, about 0.8 percentage points of
  the fleet today. Over the gap it was not counted at all.
- **Handling:** `dim_generator` shows it as `active`, with `missing_days = 1,196`. The gap is one
  `dq_issue` warning. The metric counts the unit as EIA does. An analyst who wants "operating fleet
  only" can exclude it by joining `dim_generator`.

### A5. Capacity changes every Apr 1 and Oct 1 (summer and winter ratings)

- **Example:** 68–84 units change rating on every Apr 1 and Oct 1 since 2007. In 2025-04-01, 77 units
  dropped (Wolf Creek −78.3 MW, Peach Bottom 2 −76.3 MW…). Every year, national capacity steps down
  ~1.5–2.4 GW on Apr 1 and back up on Oct 1 (A5).
- **Outliers combine with other findings:**
  - 2021-04-01 (−3,357.5 MW): Indian Point 3 (1,038.3 MW) stops being reported on the same day;
  - 2024-04-01 (only −1,195.9 MW): Vogtle 4 (+1,114 MW) returns from its gap that day.
- **Why it matters:** this is not an error, but "fleet capacity" is not comparable across seasons.
  A capacity drop on Apr 1 is not an outage.
- **Handling:** US-08 divides by the same day's capacity, so the percentage is unaffected. This is
  documented here and not flagged as a DQ issue. `unit_lifecycle` separates real fleet changes from
  seasonal ones.

### A6. `percentOutage` is rounded at facility and national level, but not per unit

- **Example:** the largest difference between EIA's value and `100 × outage / capacity` is
  2.8e-14 for generators, but 0.005 for facilities and national (A6). Facility and national values are
  rounded to 2 decimals; unit values are not. Together with A3, EIA's percentage is not one
  consistent definition across the grains.
- **Handling:** the model recomputes `percent_outage` from MW, the same way for every grain, and
  keeps EIA's value as `percent_outage_eia`. `percent_mismatch` flags any difference beyond
  rounding (none today).

### A7. Retirements have no marker; rows just stop

- **Example:** 12 units stop being reported, with no status field (A7). Four of them end on the
  same day, **2012-12-31**: San Onofre 2 and 3, Crystal River 3 and Kewaunee. Kewaunee's last row
  shows 574 MW and 0% outage, a unit running normally. Three units are first reported later than
  2007: Watts Bar 2 (2015-10-27), Vogtle 3 (2022-10-13) and Vogtle 4 (2023-08-21).
- **Why it matters:** a fleet total that drops overnight looks like an outage unless you know a
  unit left the data. One date shared by four plants again suggests a data boundary, not four
  shutdowns on one day.
- **Handling:**
  - `dim_generator.status = 'retired'` when a unit's rows stop;
  - its lifetime ends at its last row, so US-08 never counts it as missing afterwards;
  - `unit_lifecycle` writes an info row in `dq_issue` for every first and last appearance.

## Also checked, and clean

- No duplicate natural keys, negative values, or outage greater than capacity in any grain.
- No national day missing between 2007-01-01 and 2026-10-16.
- No plant changes its name. Generator ids are only `1`–`4`.
- EIA sends at most 3 decimals for MW, so `DECIMAL(12,3)` is exact.
- **Revisions:** the full load re-fetched the 46 days already stored by the first run, 18 hours
  later: 0 rows changed. The refresh of 2026-10-19 re-fetched 2026-09-16 → 10-19 (the 30-day
  lookback), 30 hours after the full load: again 0 rows changed, and 0 new days (EIA's latest day was
  still 2026-10-16). Two quiet re-fetches are too little evidence to confirm or size the 30-day
  lookback (DECISIONS.md #2); every run's `updated` count in `data/_meta/runs/` will show it over time.
