"""Model build against small raw extracts written to a temp dir (no network)."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from outage_explorer.config import Settings
from outage_explorer.connector.extract import ExtractionLockedError
from outage_explorer.model import build as build_mod
from outage_explorer.model.build import ModelBuildError, build_model, load_current, table_globs
from outage_explorer.model.tables import TABLES
from tests.conftest import fleet_rows, query_model

DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
UNITS = {("46", "1"): 1000.0, ("46", "2"): 1100.5, ("204", "1"): 800.25}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path)


def test_builds_every_table_with_declared_types(settings, write_fleet):
    write_fleet(fleet_rows(UNITS, DAYS))
    manifest = build_model(settings)

    assert set(manifest["tables"]) == set(TABLES)
    assert manifest["tables"]["fact_generator_daily"]["rows"] == 15
    assert manifest["tables"]["dim_facility"]["rows"] == 2
    con = duckdb.connect()
    for name, glob in table_globs(settings.data_path).items():
        types = dict(
            con.execute(
                f"SELECT column_name, column_type FROM (DESCRIBE FROM read_parquet('{glob}'))"
            ).fetchall()
        )
        for col in TABLES[name].columns:
            assert types[col.name] == col.type, (name, col.name)


def test_grains_reconcile_exactly_with_decimals(settings, write_fleet):
    # 0.1 + 0.2 + 0.3 is not 0.6 in floating point; as DECIMAL every grain agrees exactly.
    units = {("46", "1"): 0.1, ("46", "2"): 0.2, ("204", "1"): 0.3}
    write_fleet(fleet_rows(units, DAYS[:1]))
    build_model(settings)
    [(nat, fac, gen)] = query_model(
        settings,
        """SELECT (SELECT capacity_mw FROM fact_national_daily),
                  (SELECT sum(capacity_mw) FROM fact_facility_daily),
                  (SELECT sum(capacity_mw) FROM fact_generator_daily)""",
    )
    assert nat == fac == gen == Decimal("0.600")


def test_percent_is_recomputed_and_null_without_capacity(settings, write_fleet):
    write_fleet(fleet_rows({("2497", "2"): 1000.0}, DAYS[:1], outages={(DAYS[0], "2497", "2"): 250.0}))
    # Indian Point 2 in Q1 2020: capacity 0 but EIA still reports a percentage.
    zero = fleet_rows({("2497", "2"): 0.0}, DAYS[1:2])
    zero["generator"][0]["percent_outage"] = 11.0
    write_fleet(zero)
    build_model(settings)
    got = query_model(
        settings, "SELECT period, percent_outage, percent_outage_eia FROM fact_generator_daily ORDER BY 1"
    )
    assert got == [(DAYS[0], 25.0, Decimal("25.00")), (DAYS[1], None, Decimal("11.00"))]


def test_duplicate_raw_keys_keep_the_latest_ingested_row(settings, write_fleet, write_raw):
    write_fleet(fleet_rows(UNITS, DAYS))
    stale = {"period": DAYS[0], "capacity": 1.0, "outage": 1.0, "percent_outage": 100.0}
    write_raw("national", [stale], run_id="r0", ingested_at=datetime(2025, 1, 1))
    manifest = build_model(settings)

    assert manifest["source"]["national"]["duplicates_dropped"] == 1
    [(cap,)] = query_model(
        settings, f"SELECT capacity_mw FROM fact_national_daily WHERE period = '{DAYS[0]}'"
    )
    assert cap == Decimal("2900.750")


def test_primary_key_violation_fails_the_build(settings, write_fleet, write_raw, monkeypatch):
    # Mutation check: without the dedupe step, the PRIMARY KEY constraint must stop the build.
    write_fleet(fleet_rows(UNITS, DAYS))
    write_raw("national", [{"period": DAYS[0], "capacity": 1.0, "outage": 1.0}], run_id="r0")
    no_dedupe = build_mod._SELECTS["fact_national_daily"].replace("src_national", "raw_national")
    monkeypatch.setitem(build_mod._SELECTS, "fact_national_daily", no_dedupe)
    with pytest.raises(ModelBuildError, match="fact_national_daily"):
        build_model(settings)
    assert load_current(settings.data_path) is None


def test_dimensions_track_lifetime_gaps_and_retirement(settings, write_fleet):
    units = {("6462", "1"): 967.0, ("360", "2"): 1070.0, ("46", "1"): 1000.0}
    missing = {(DAYS[2], "6462", "1"), (DAYS[3], "6462", "1")} | {(d, "360", "2") for d in DAYS[3:]}
    write_fleet(fleet_rows(units, DAYS, missing=missing))
    build_model(settings)
    got = query_model(
        settings,
        """SELECT facility_id, first_period, last_period, reported_days, missing_days, status
           FROM dim_generator ORDER BY facility_id""",
    )
    assert got == [
        ("360", DAYS[0], DAYS[2], 3, 0, "retired"),
        ("46", DAYS[0], DAYS[4], 5, 0, "active"),
        ("6462", DAYS[0], DAYS[4], 3, 2, "active"),
    ]


def test_new_build_switches_current_and_prunes_old_ones(settings, write_fleet):
    write_fleet(fleet_rows(UNITS, DAYS))
    ids = [build_model(settings, keep=2)["build_id"] for _ in range(3)]

    current = load_current(settings.data_path)
    assert current["build_id"] == ids[-1]
    builds = sorted(p.name for p in (settings.data_path / "model" / "builds").iterdir())
    assert builds == ids[1:]  # oldest pruned, no temp folders left
    on_disk = json.loads((settings.data_path / "model" / "current.json").read_text(encoding="utf-8"))
    assert on_disk == current


def test_failed_build_keeps_the_previous_model(settings, write_fleet):
    write_fleet(fleet_rows(UNITS, DAYS))
    first = build_model(settings)["build_id"]
    for f in (settings.data_path / "raw" / "generator").rglob("*.parquet"):
        f.unlink()
    with pytest.raises(ModelBuildError, match="run `uv run outage-extract` first"):
        build_model(settings)
    assert load_current(settings.data_path)["build_id"] == first
    assert [p.name for p in (settings.data_path / "model" / "builds").iterdir()] == [first]


def test_build_refuses_to_run_while_an_extraction_holds_the_lock(settings, write_fleet):
    write_fleet(fleet_rows(UNITS, DAYS))
    lock = settings.data_path / "_meta" / "extract.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("run-123", encoding="utf-8")
    with pytest.raises(ExtractionLockedError, match="run-123"):
        build_model(settings)
    assert load_current(settings.data_path) is None
