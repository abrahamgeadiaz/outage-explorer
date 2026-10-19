"""outage-sql: block parsing, the DuckDB export for database managers, and the FINDINGS.md queries."""

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from outage_explorer.config import Settings
from outage_explorer.model import explore
from outage_explorer.model.build import build_model
from outage_explorer.model.tables import TABLES
from tests.conftest import fleet_rows

FINDINGS_SQL = Path(__file__).resolve().parents[2] / "analysis" / "findings.sql"
DAYS = [date(2015, 10, 1) + timedelta(days=i) for i in range(3)]


@pytest.fixture
def built(tmp_path, write_fleet, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    units = {("6462", "1"): 967.0, ("46", "1"): 1000.0}
    write_fleet(fleet_rows(units, DAYS, missing={(DAYS[1], "6462", "1")}))
    build_model(settings)
    monkeypatch.setattr(explore, "get_settings", lambda: settings)
    return settings


def test_blocks_split_on_name_comments():
    sql = "-- header, ignored\n-- name: first\nSELECT 1;\n\n-- name: second one\nSELECT 2\n"
    assert explore.blocks(sql) == [("first", "SELECT 1;"), ("second one", "SELECT 2")]


def test_every_findings_query_runs_on_the_model(built):
    con = duckdb.connect()
    explore.create_views(con, built.data_path)
    named = explore.blocks(FINDINGS_SQL.read_text(encoding="utf-8"))
    assert len(named) >= 15
    for name, statement in named:
        con.sql(statement).fetchall()  # raises if a query no longer matches the model
        assert name


def test_duckdb_export_has_model_and_raw_views(built, tmp_path, capsys):
    target = tmp_path / "explore.duckdb"
    assert explore.main(["--duckdb", str(target)]) == 0
    with duckdb.connect(str(target), read_only=True) as con:
        views = {
            r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()
        }
        assert views == set(TABLES) | {"raw_national", "raw_facility", "raw_generator"}
        [(missing,)] = con.execute(
            f"SELECT missing_units FROM metric_fleet_offline_daily WHERE period = '{DAYS[1]}'"
        ).fetchall()
    assert missing == ["6462/1"]
    assert "views written" in capsys.readouterr().out


def test_without_a_model_it_explains_what_to_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(explore, "get_settings", lambda: Settings(data_dir=tmp_path))
    sql = tmp_path / "q.sql"
    sql.write_text("-- name: q\nSELECT 1", encoding="utf-8")
    assert explore.main([str(sql)]) == 2
    assert "uv run outage-model" in capsys.readouterr().err
