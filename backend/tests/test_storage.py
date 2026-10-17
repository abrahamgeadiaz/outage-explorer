from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer.connector.routes import FACILITY
from outage_explorer.connector.storage import UnsafeOverwriteError, partition_path, upsert_window
from outage_explorer.connector.validate import schema_for

KEYS = FACILITY.key_columns
T1, T2 = datetime(2026, 10, 1, 12), datetime(2026, 10, 2, 12)


def table(rows):
    full = [{"facility_name": "Plant " + r["facility"], "percent_outage": None, **r} for r in rows]
    return pa.Table.from_pylist(full, schema=schema_for(FACILITY))


def row(day, facility, outage, capacity=1000.0):
    return {"period": date(2026, 10, day), "facility": facility, "capacity": capacity, "outage": outage}


@pytest.fixture
def path(tmp_path):
    return partition_path(tmp_path / "raw", "facility", 2026)


def read(path):
    return sorted(pq.read_table(path).to_pylist(), key=lambda r: (r["period"], r["facility"]))


def upsert(path, rows, start=1, end=31, run="r1", at=T1):
    return upsert_window(
        path, table(rows), KEYS, date(2026, 10, start), date(2026, 10, end), run_id=run, ingested_at=at
    )


def test_first_write_creates_partition_with_lineage(path):
    stats = upsert(path, [row(1, "46", 0.0), row(1, "204", 50.0)])
    assert stats.inserted == 2 and stats.rows_after == 2
    rows = read(path)
    assert {r["_run_id"] for r in rows} == {"r1"}
    assert path.parent.name == "year=2026"


def test_rerun_with_same_data_is_idempotent(path):
    data = [row(1, "46", 0.0), row(2, "46", 10.0)]
    upsert(path, data)
    before = read(path)
    stats = upsert(path, data, run="r2", at=T2)
    assert stats.unchanged == 2 and stats.inserted == stats.updated == stats.deleted == 0
    assert read(path) == before  # unchanged rows keep r1 lineage


def test_revised_value_replaces_old_and_is_counted(path):
    upsert(path, [row(1, "46", 0.0), row(2, "46", 10.0)])
    stats = upsert(path, [row(1, "46", 0.0), row(2, "46", 99.0)], run="r2", at=T2)
    assert stats.updated == 1 and stats.unchanged == 1
    rows = read(path)
    assert len(rows) == 2
    assert rows[1]["outage"] == 99.0 and rows[1]["_run_id"] == "r2"
    assert rows[0]["_run_id"] == "r1"


def test_rows_outside_window_are_untouched(path):
    upsert(path, [row(1, "46", 5.0), row(20, "46", 7.0)])
    upsert(path, [row(20, "46", 8.0)], start=15, end=31, run="r2", at=T2)
    rows = read(path)
    assert [(r["period"].day, r["outage"]) for r in rows] == [(1, 5.0), (20, 8.0)]


def test_row_missing_from_source_inside_window_is_deleted_and_counted(path):
    upsert(path, [row(1, "46", 0.0), row(1, "204", 0.0)])
    stats = upsert(path, [row(1, "46", 0.0)], run="r2", at=T2)
    assert stats.deleted == 1
    assert [r["facility"] for r in read(path)] == ["46"]


def test_empty_fetch_never_wipes_stored_window(path):
    upsert(path, [row(1, "46", 0.0)])
    with pytest.raises(UnsafeOverwriteError):
        upsert(path, [], run="r2", at=T2)
    assert len(read(path)) == 1


def test_empty_fetch_on_empty_window_is_fine(path):
    upsert(path, [row(1, "46", 0.0)], start=1, end=1)
    stats = upsert(path, [], start=10, end=31, run="r2", at=T2)
    assert stats.rows_after == 1


def test_no_temp_files_left_behind(path):
    upsert(path, [row(1, "46", 0.0)])
    upsert(path, [row(1, "46", 1.0)], run="r2", at=T2)
    assert [p.name for p in path.parent.iterdir()] == ["data.parquet"]
