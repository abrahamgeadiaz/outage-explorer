"""US-08 metric (metric_fleet_offline_daily) on small fleets with the anomalies found in the real data."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from outage_explorer.config import Settings
from outage_explorer.model.build import build_model
from tests.conftest import fleet_rows, query_model

D = [date(2015, 2, 12) + timedelta(days=i) for i in range(5)]
COLS = """offline_pct, offline_pct_min, offline_pct_max, units_reported, units_missing, missing_units,
          missing_capacity_mw, units_zero_capacity, is_complete"""


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


def metric(settings, day: date) -> dict:
    sql = f"SELECT {COLS} FROM metric_fleet_offline_daily WHERE period = '{day}'"
    [row] = query_model(settings, sql)
    return dict(zip([c.strip() for c in COLS.split(",")], row, strict=True))


def test_complete_day_is_outage_over_capacity_and_matches_eia(settings, write_fleet):
    units = {("46", "1"): 1000.0, ("204", "1"): 1500.0}
    write_fleet(fleet_rows(units, D[:1], outages={(D[0], "204", "1"): 300.0}))
    build_model(settings)

    m = metric(settings, D[0])
    assert m["offline_pct"] == pytest.approx(12.0)
    assert m["offline_pct_min"] == m["offline_pct_max"] == m["offline_pct"]
    assert (m["units_reported"], m["units_missing"], m["missing_units"]) == (2, 0, [])
    assert m["is_complete"] is True
    [(eia,)] = query_model(settings, "SELECT percent_outage_eia FROM metric_fleet_offline_daily")
    assert eia == Decimal("12.00")


def test_missing_unit_is_not_filled_in_but_sized_and_bounded(settings, write_fleet):
    # River Bend on 2015-02-14: absent from every grain, so EIA's national capacity drops by 967 MW.
    units = {("6462", "1"): 967.0, ("46", "1"): 1033.0}
    outages = {(d, "46", "1"): 100.0 for d in D}
    write_fleet(fleet_rows(units, D, outages=outages, missing={(D[2], "6462", "1")}))
    build_model(settings)

    m = metric(settings, D[2])
    assert m["offline_pct"] == pytest.approx(100 * 100 / 1033)  # reported units only, like EIA
    assert m["units_missing"] == 1 and m["missing_units"] == ["6462/1"]
    assert m["missing_capacity_mw"] == Decimal("967.000")
    assert m["offline_pct_min"] == pytest.approx(100 * 100 / 2000)  # River Bend fully online
    assert m["offline_pct_max"] == pytest.approx(100 * 1067 / 2000)  # River Bend fully offline
    assert m["is_complete"] is False
    assert metric(settings, D[3])["is_complete"] is True


def test_missing_capacity_is_the_last_value_before_the_gap(settings, write_fleet):
    # Ratings change (seasonal Apr 1 / Oct 1); the gap must be sized with the rating in force before it.
    write_fleet(fleet_rows({("6462", "1"): 900.0, ("46", "1"): 1000.0}, D[:2]))
    write_fleet(fleet_rows({("46", "1"): 1000.0}, D[2:3]))
    write_fleet(fleet_rows({("6462", "1"): 950.0, ("46", "1"): 1000.0}, D[3:]))
    build_model(settings)
    assert metric(settings, D[2])["missing_capacity_mw"] == Decimal("900.000")


def test_zero_capacity_unit_is_left_out_of_the_share(settings, write_fleet):
    # Indian Point 2, Q1 2020: capacity 0 while EIA still reports a percentage for it.
    rows = fleet_rows({("2497", "2"): 0.0, ("46", "1"): 1000.0}, D[:1], outages={(D[0], "46", "1"): 50.0})
    rows["generator"][0]["percent_outage"] = 11.0
    write_fleet(rows)
    build_model(settings)

    m = metric(settings, D[0])
    assert m["offline_pct"] == pytest.approx(5.0)
    assert (m["units_reported"], m["units_zero_capacity"], m["is_complete"]) == (1, 1, False)


def test_units_outside_their_lifetime_are_not_missing(settings, write_fleet):
    # Retired before D[3] (San Onofre style) and first reported on D[2] (Vogtle 3 style).
    units = {("46", "1"): 1000.0, ("360", "2"): 1070.0, ("649", "3"): 1114.0}
    missing = {(d, "360", "2") for d in D[3:]} | {(d, "649", "3") for d in D[:2]}
    write_fleet(fleet_rows(units, D, missing=missing))
    build_model(settings)

    rows = query_model(
        settings, "SELECT period, units_reported, units_missing FROM metric_fleet_offline_daily"
    )
    assert sorted(rows) == [(D[0], 2, 0), (D[1], 2, 0), (D[2], 3, 0), (D[3], 2, 0), (D[4], 2, 0)]
