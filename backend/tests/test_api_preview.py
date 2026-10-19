"""Data preview (US-03) and the per-grant serving databases behind it (US-05)."""

import duckdb
import pytest

from outage_explorer.api import store
from outage_explorer.api.policy import Role, granted_tables
from outage_explorer.model.build import build_model, table_globs
from tests.conftest import API_DAYS, auth_headers

ROWS = "/api/datasets/{}/rows"


def _get(client, user, dataset, **params):
    return client.get(ROWS.format(dataset), params=params, headers=auth_headers(client, user))


def _keys(body, *names):
    idx = [[c["name"] for c in body["columns"]].index(n) for n in names]
    return [tuple(row[i] for i in idx) for row in body["rows"]]


def test_analyst_previews_generator_rows_in_key_order(client, api_model):
    body = _get(client, "analyst", "fact_generator_daily").json()
    assert body["build_id"] == api_model["build_id"]
    assert body["page"] == {"limit": 100, "offset": 0, "total": 8, "has_more": False}
    keys = _keys(body, "period", "facility_id", "generator_id")
    assert keys == sorted(keys)
    assert ("2015-10-02", "6462", "1") not in keys  # River Bend is missing that day
    row = dict(zip([c["name"] for c in body["columns"]], body["rows"][0], strict=True))
    assert row["capacity_mw"] == 1000.0  # DECIMAL served as a JSON number
    assert row["outage_mw"] == 0.0


def test_date_filter_is_inclusive(client, api_model):
    day = API_DAYS[1].isoformat()
    body = _get(client, "analyst", "fact_generator_daily", start=day, end=day).json()
    assert _keys(body, "period", "facility_id", "generator_id") == [(day, "46", "1"), (day, "46", "2")]


def test_facility_filter_takes_one_or_several_ids(client, api_model):
    one = _get(client, "analyst", "fact_facility_daily", facility_id="6462").json()
    assert _keys(one, "period", "facility_id") == [("2015-10-01", "6462"), ("2015-10-03", "6462")]
    both = _get(client, "analyst", "fact_facility_daily", facility_id=["6462", "46"]).json()
    assert both["page"]["total"] == 5


def test_date_filter_on_a_span_keeps_overlapping_rows(client, api_model):
    assert _get(client, "analyst", "dim_generator", start="2015-10-03").json()["page"]["total"] == 3
    assert _get(client, "analyst", "dim_generator", start="2015-10-04").json()["page"]["total"] == 0
    gap = _get(client, "analyst", "dq_issue", start="2015-10-02", end="2015-10-02", facility_id="6462").json()
    assert ("unit_reporting_gap", "2015-10-02") in _keys(gap, "check_name", "period_start")


def test_pages_are_disjoint_and_cover_every_row(client, api_model):
    everything = _get(client, "analyst", "fact_generator_daily").json()["rows"]
    pages, offset = [], 0
    while True:
        body = _get(client, "analyst", "fact_generator_daily", limit=3, offset=offset).json()
        pages.extend(body["rows"])
        offset += 3
        if not body["page"]["has_more"]:
            break
    assert pages == everything
    assert body["page"] == {"limit": 3, "offset": 6, "total": 8, "has_more": False}


def test_filter_values_are_parameters_not_sql(client, api_model):
    body = _get(client, "analyst", "fact_facility_daily", facility_id="46' OR '1'='1").json()
    assert body["page"]["total"] == 0


@pytest.mark.parametrize(
    "dataset,params,code",
    [
        ("fact_national_daily", {"facility_id": "46"}, "filter_not_supported"),
        ("fact_generator_daily", {"start": "2015-10-03", "end": "2015-10-01"}, "invalid_filter"),
        ("fact_generator_daily", {"facility_id": [str(i) for i in range(101)]}, "invalid_filter"),
    ],
)
def test_bad_filters_are_400(client, api_model, dataset, params, code):
    resp = _get(client, "analyst", dataset, **params)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == code


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1001}, {"offset": -1}, {"start": "yesterday"}])
def test_out_of_range_parameters_are_422(client, api_model, params):
    assert _get(client, "analyst", "fact_generator_daily", **params).status_code == 422


def test_viewer_previews_national_data(client, api_model):
    body = _get(client, "viewer", "fact_national_daily").json()
    assert body["page"]["total"] == 3


def test_viewer_gets_the_metric_without_unit_names(client, api_model):
    body = _get(client, "viewer", "metric_fleet_offline_daily").json()
    names = [c["name"] for c in body["columns"]]
    assert "missing_units" not in names
    assert all(len(row) == len(names) for row in body["rows"])
    assert "6462" not in str(body["rows"])  # the missing unit is counted, never named
    assert _keys(body, "units_missing") == [(0,), (1,), (0,)]


@pytest.mark.parametrize(
    "dataset", ["fact_facility_daily", "fact_generator_daily", "dim_facility", "dim_generator", "dq_issue"]
)
def test_viewer_cannot_preview_detail_datasets(client, api_model, dataset):
    resp = _get(client, "viewer", dataset)
    assert resp.status_code == 403
    assert "6462" not in resp.text


def test_preview_requires_login_and_a_known_dataset(client, api_model):
    assert client.get(ROWS.format("fact_national_daily")).status_code == 401
    assert _get(client, "analyst", "no_such_table").status_code == 404


def test_without_a_model_preview_says_so(client):
    resp = _get(client, "analyst", "fact_national_daily")
    assert resp.status_code == 503


# --- serving databases ---------------------------------------------------------------------------


def _tables_in(path) -> dict[str, list[str]]:
    with duckdb.connect(str(path), read_only=True) as con:
        rows = con.execute(
            "SELECT table_name, column_name FROM duckdb_columns() WHERE NOT internal ORDER BY column_index"
        )
        out: dict[str, list[str]] = {}
        for table, column in rows.fetchall():
            out.setdefault(table, []).append(column)
        return out


def test_viewer_serving_file_holds_only_the_viewer_grant(api_settings, api_model):
    grant = granted_tables(Role.VIEWER)
    store.connect(api_settings.data_path, api_model, grant).close()
    path = (
        store.serve_dir(api_settings.data_path) / api_model["build_id"] / f"{store.grant_key(grant)}.duckdb"
    )
    assert _tables_in(path) == {t: [c.name for c in cols] for t, cols in grant.items()}


def test_analyst_and_admin_share_a_file_and_viewer_has_its_own(api_settings):
    keys = {role: store.grant_key(granted_tables(role)) for role in Role}
    assert keys[Role.ANALYST] == keys[Role.ADMIN] != keys[Role.VIEWER]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t AS SELECT 1",
        "DELETE FROM fact_national_daily",
        "SET enable_external_access = true",
        "ATTACH ':memory:' AS m",
        "COPY fact_national_daily TO '{tmp}/out.csv'",
        "SELECT * FROM read_parquet('{generator}')",  # the Viewer's way around the grant, if files were open
    ],
)
def test_serving_connection_is_read_only_and_cannot_reach_files(api_settings, api_model, tmp_path, sql):
    generator = table_globs(api_settings.data_path)["fact_generator_daily"]
    with store.connect(api_settings.data_path, api_model, granted_tables(Role.VIEWER)) as con:
        with pytest.raises(duckdb.Error):
            con.execute(sql.format(generator=generator, tmp=tmp_path.as_posix()))


def test_serving_connection_has_resource_limits_a_query_cannot_lift(api_settings, api_model):
    with store.connect(api_settings.data_path, api_model, granted_tables(Role.VIEWER)) as con:
        settings = con.execute(
            "SELECT current_setting('memory_limit'), current_setting('threads'),"
            " current_setting('max_temp_directory_size')"
        ).fetchone()
        assert settings == ("953.6 MiB", 2, "0 bytes")
        with pytest.raises(duckdb.Error, match="locked"):
            con.execute("SET memory_limit = '8GB'")


def test_a_query_over_the_memory_limit_fails_instead_of_growing(api_settings, api_model):
    # An aggregate's state is held by DuckDB's buffer manager, so the limit applies to it.
    sql = "SELECT length(string_agg(repeat('x', 1000000), '')) FROM range(2000)"
    with store.connect(api_settings.data_path, api_model, granted_tables(Role.VIEWER)) as con:
        with pytest.raises(duckdb.OutOfMemoryException):
            con.execute(sql)


def test_a_new_build_gets_a_new_file_and_old_ones_are_removed(api_settings, api_model):
    grant = granted_tables(Role.ANALYST)
    store.connect(api_settings.data_path, api_model, grant).close()
    newer = build_model(api_settings)
    store.connect(api_settings.data_path, newer, grant).close()
    assert [p.name for p in store.serve_dir(api_settings.data_path).iterdir()] == [newer["build_id"]]


def test_a_viewer_request_opens_the_viewer_file(client, api_settings, api_model):
    _get(client, "viewer", "fact_national_daily")
    files = [p.name for p in (store.serve_dir(api_settings.data_path) / api_model["build_id"]).iterdir()]
    assert files == [f"{store.grant_key(granted_tables(Role.VIEWER))}.duckdb"]
