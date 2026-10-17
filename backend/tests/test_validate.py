from datetime import date

import pyarrow as pa
import pytest

from outage_explorer.connector.routes import FACILITY, GENERATOR, NATIONAL
from outage_explorer.connector.validate import validate_rows

START, END = date(2026, 10, 1), date(2026, 10, 31)


def gen_row(**overrides):
    row = {
        "period": "2026-10-16",
        "facility": "46",
        "facilityName": "Browns Ferry",
        "generator": "1",
        "capacity": "1254.8",
        "outage": "0",
        "percentOutage": "0",
        "capacity-units": "megawatts",
        "outage-units": "megawatts",
        "percentOutage-units": "percent",
    }
    row.update(overrides)
    return row


def test_recorded_rows_are_typed(fixture_json):
    rows = fixture_json("generator_sample.json")["response"]["data"]
    result = validate_rows(GENERATOR, rows, START, END)
    assert result.rows_rejected == 0
    assert result.table.num_rows == len(rows)
    assert result.table.schema.field("period").type == pa.date32()
    assert result.table.schema.field("capacity").type == pa.float64()
    assert result.table.column_names == [
        "period", "facility", "generator", "facility_name", "capacity", "outage", "percent_outage",
    ]  # fmt: skip
    assert result.unknown_fields == set()


def test_national_has_no_facility_columns(fixture_json):
    rows = fixture_json("national_3days.json")["response"]["data"]
    result = validate_rows(NATIONAL, rows, START, END)
    assert result.table.column_names == ["period", "capacity", "outage", "percent_outage"]
    assert result.table.num_rows == 3


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"period": None}, "invalid_period"),
        ({"period": "2026-13-01"}, "invalid_period"),
        ({"period": "2026-09-30"}, "period_out_of_window"),
        ({"facility": " "}, "missing_facility"),
        ({"generator": None}, "missing_generator"),
        ({"capacity": None}, "missing_capacity"),
        ({"capacity": "n/a"}, "invalid_capacity"),
        ({"outage": "NaN"}, "invalid_outage"),
        ({"capacity-units": "kilowatts"}, "unexpected_capacity-units"),
    ],
)
def test_invalid_rows_are_rejected_with_reason(overrides, reason):
    result = validate_rows(GENERATOR, [gen_row(), gen_row(**{"generator": "2", **overrides})], START, END)
    assert result.table.num_rows == 1
    assert result.rejected == {reason: 1}
    assert result.rejected_examples[0]["reason"] == reason


def test_bad_percent_is_kept_as_null_with_warning():
    result = validate_rows(GENERATOR, [gen_row(percentOutage="abc")], START, END)
    assert result.table.num_rows == 1
    assert result.table.column("percent_outage")[0].as_py() is None
    assert result.warnings["invalid_percentOutage"] == 1


def test_semantic_anomalies_are_kept_and_counted():
    rows = [gen_row(outage="2000"), gen_row(generator="2", capacity="-5", outage="-5")]
    result = validate_rows(GENERATOR, rows, START, END)
    assert result.table.num_rows == 2
    assert result.warnings["outage_exceeds_capacity"] == 1
    assert result.warnings["negative_capacity"] == 1
    assert result.warnings["negative_outage"] == 1


def test_duplicate_keys_keep_last_and_are_counted():
    rows = [gen_row(outage="10"), gen_row(outage="20")]
    result = validate_rows(GENERATOR, rows, START, END)
    assert result.table.num_rows == 1
    assert result.table.column("outage")[0].as_py() == 20.0
    assert result.warnings["duplicate_key"] == 1


def test_same_facility_different_generator_is_not_a_duplicate():
    result = validate_rows(GENERATOR, [gen_row(generator="1"), gen_row(generator="2")], START, END)
    assert result.table.num_rows == 2


def test_unknown_fields_are_reported_for_schema_drift():
    result = validate_rows(FACILITY, [{**gen_row(), "newColumn": "x"}], START, END)
    assert "newColumn" in result.unknown_fields
    assert "generator" not in result.unknown_fields  # known field, just unused by this route


def test_empty_input_gives_empty_typed_table():
    result = validate_rows(FACILITY, [], START, END)
    assert result.table.num_rows == 0
    assert "facility" in result.table.column_names
