"""Data quality checks: a clean fleet has no findings; each anomaly from the real data is caught."""

from datetime import date, datetime, timedelta

import pytest

from outage_explorer.config import Settings
from outage_explorer.model.build import build_model
from outage_explorer.model.quality import CHECKS
from tests.conftest import fleet_rows, query_model

D = [date(2020, 3, 14) + timedelta(days=i) for i in range(6)]
UNITS = {("46", "1"): 1000.0, ("46", "2"): 1100.0, ("204", "1"): 800.0}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


def issues(settings) -> list[dict]:
    cols = "check_name, severity, dataset, facility_id, generator_id, period_start, period_end, days, detail"
    rows = query_model(settings, f"SELECT {cols} FROM dq_issue ORDER BY issue_id")
    return [dict(zip(cols.split(", "), r, strict=True)) for r in rows]


def only(settings, check: str) -> list[dict]:
    return [i for i in issues(settings) if i["check_name"] == check]


def test_a_clean_fleet_has_no_findings(settings, write_fleet):
    write_fleet(fleet_rows(UNITS, D, outages={(D[1], "46", "2"): 550.0}))
    manifest = build_model(settings)
    assert issues(settings) == []
    assert manifest["data_quality"] == {}


def test_every_check_has_a_description():
    assert len({c.name for c in CHECKS}) == len(CHECKS)
    assert all(c.description for c in CHECKS)


def test_national_that_does_not_add_up_is_an_error(settings, write_fleet):
    rows = fleet_rows(UNITS, D)
    rows["national"][2]["capacity"] += 5.0
    write_fleet(rows)
    build_model(settings)

    found = only(settings, "grain_mismatch_national")
    assert {(i["period_start"], i["severity"]) for i in found} == {(D[2], "error")}
    assert len(found) == 2  # vs facility and vs generator, capacity only
    assert "gap 5.000 MW" in found[0]["detail"]


def test_generators_without_a_facility_row_are_reported(settings, write_fleet):
    # DECISIONS.md #3: the generator rows are kept; the missing facility row is a finding.
    rows = fleet_rows(UNITS, D)
    rows["facility"] = [r for r in rows["facility"] if not (r["facility"] == "204" and r["period"] == D[0])]
    write_fleet(rows)
    build_model(settings)

    found = only(settings, "grain_mismatch_facility")
    assert {(i["facility_id"], i["period_start"]) for i in found} == {("204", D[0])}
    assert found[0]["detail"] == "generators reported but no facility row"
    # The national row still equals Σ generators, so it differs from Σ facility: reported as well.
    assert {i["detail"].split(":")[0] for i in only(settings, "grain_mismatch_national")} == {
        "national capacity_mw vs sum of facility rows"
    }


def test_facility_that_differs_from_its_generators_is_reported(settings, write_fleet):
    rows = fleet_rows(UNITS, D)
    fac = next(r for r in rows["facility"] if r["facility"] == "46" and r["period"] == D[3])
    fac["outage"] = 10.0
    write_fleet(rows)
    build_model(settings)

    [issue] = only(settings, "grain_mismatch_facility")
    assert (issue["facility_id"], issue["period_start"]) == ("46", D[3])
    assert issue["detail"] == "facility outage_mw vs sum of its generators: gap 10.000 MW"


def test_unit_missing_from_every_grain_is_one_warning_per_gap(settings, write_fleet):
    # River Bend style: two consecutive days absent, then back.
    write_fleet(fleet_rows(UNITS, D, missing={(D[2], "204", "1"), (D[3], "204", "1")}))
    build_model(settings)

    [gap] = only(settings, "unit_reporting_gap")
    assert (gap["severity"], gap["facility_id"], gap["generator_id"]) == ("warning", "204", "1")
    assert (gap["period_start"], gap["period_end"], gap["days"]) == (D[2], D[3], 2)
    assert "800.000 MW missing" in gap["detail"]
    assert only(settings, "grain_mismatch_national") == []  # EIA's sums still add up


def test_a_single_missing_day_is_a_gap(settings, write_fleet):
    # River Bend on 2015-10-14: one day absent between two reported days.
    write_fleet(fleet_rows(UNITS, D, missing={(D[2], "46", "2")}))
    build_model(settings)
    [gap] = only(settings, "unit_reporting_gap")
    assert (gap["generator_id"], gap["period_start"], gap["days"]) == ("2", D[2], 1)


def test_zero_capacity_with_a_percentage_is_an_error(settings, write_fleet):
    # Indian Point 2, Q1 2020: capacity 0 for weeks, a percentage reported for the last days.
    rows = fleet_rows({**UNITS, ("2497", "2"): 0.0}, D)
    for r in rows["generator"]:
        if r["facility"] == "2497" and r["period"] >= D[4]:
            r["percent_outage"] = 10.0
    write_fleet(rows)
    build_model(settings)

    [issue] = only(settings, "non_positive_capacity")
    assert (issue["severity"], issue["period_start"], issue["period_end"]) == ("error", D[0], D[5])
    assert issue["detail"] == f"capacity 0 MW; EIA still reports up to 10.00% outage from {D[4]}"


def test_zero_capacity_alone_is_a_warning(settings, write_fleet):
    write_fleet(fleet_rows({**UNITS, ("2497", "2"): 0.0}, D[:2]))
    build_model(settings)
    [issue] = only(settings, "non_positive_capacity")
    assert issue["severity"] == "warning"


def test_percent_beyond_rounding_is_an_error_but_rounding_is_not(settings, write_fleet):
    rows = fleet_rows({("46", "1"): 3000.0}, D[:2], outages={(d, "46", "1"): 100.0 for d in D[:2]})
    rows["national"][0]["percent_outage"] = 3.33  # 3.3333...: rounding, fine
    rows["national"][1]["percent_outage"] = 3.5  # wrong
    write_fleet(rows)
    build_model(settings)
    found = only(settings, "percent_mismatch")
    assert [(i["dataset"], i["period_start"]) for i in found] == [("national", D[1])]


def test_outage_above_capacity_is_an_error(settings, write_fleet):
    rows = fleet_rows(UNITS, D[:1])
    gen = next(r for r in rows["generator"] if r["facility"] == "204")
    gen["outage"], gen["percent_outage"] = 900.0, 112.5
    write_fleet(rows)
    build_model(settings)
    found = only(settings, "value_out_of_range")
    assert [(i["dataset"], i["facility_id"], i["generator_id"]) for i in found] == [("generator", "204", "1")]


def test_units_starting_late_or_stopping_are_info(settings, write_fleet):
    units = {**UNITS, ("360", "2"): 1070.0, ("649", "3"): 1114.0}
    missing = {(d, "360", "2") for d in D[3:]} | {(d, "649", "3") for d in D[:2]}
    write_fleet(fleet_rows(units, D, missing=missing))
    build_model(settings)

    found = {(i["facility_id"], i["period_start"], i["detail"]) for i in only(settings, "unit_lifecycle")}
    assert found == {
        ("360", D[2], f"unit last reported on {D[2]}"),
        ("649", D[2], f"unit first reported on {D[2]}"),
    }
    assert {i["severity"] for i in only(settings, "unit_lifecycle")} == {"info"}
    assert only(settings, "unit_reporting_gap") == []  # outside the lifetime is not a gap


def test_dropped_duplicates_are_reported(settings, write_fleet, write_raw):
    write_fleet(fleet_rows(UNITS, D))
    write_raw(
        "national", [{"period": D[0], "capacity": 1.0, "outage": 0.0}], ingested_at=datetime(2020, 1, 1)
    )
    manifest = build_model(settings)

    [issue] = only(settings, "duplicate_keys")
    assert (issue["severity"], issue["dataset"]) == ("warning", "national")
    assert issue["detail"] == "1 duplicate rows dropped"
    assert manifest["data_quality"] == {"duplicate_keys": {"warning": 1}}
