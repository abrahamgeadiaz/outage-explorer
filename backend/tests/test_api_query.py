"""POST /api/query: read-only SQL with per-table authorization, row cap and timeout (US-04, US-05)."""

import pytest
from fastapi.testclient import TestClient

from outage_explorer.api import query as query_module
from outage_explorer.api.app import create_app
from tests.conftest import auth_headers


def _query(client, user, sql, **extra):
    return client.post("/api/query", json={"sql": sql, **extra}, headers=auth_headers(client, user))


def test_analyst_runs_a_join_with_aggregates(client, api_model):
    sql = """
        SELECT g.period, f.facility_name, sum(g.capacity_mw) AS cap, sum(g.outage_mw) AS out
        FROM fact_generator_daily g JOIN dim_facility f USING (facility_id)
        WHERE g.facility_id = '46'
        GROUP BY ALL ORDER BY g.period
    """
    body = _query(client, "analyst", sql).json()
    assert body["tables"] == ["dim_facility", "fact_generator_daily"]
    assert [c["name"] for c in body["columns"]] == ["period", "facility_name", "cap", "out"]
    assert body["rows"][0] == ["2015-10-01", "Plant 46", 2100.0, 550.0]
    assert body["row_count"] == 3 and body["truncated"] is False
    assert body["build_id"] == api_model["build_id"]


def test_cte_and_window_function(client, api_model):
    sql = """
        WITH daily AS (SELECT period, offline_pct FROM metric_fleet_offline_daily)
        SELECT period, offline_pct - lag(offline_pct) OVER (ORDER BY period) AS change FROM daily ORDER BY 1
    """
    rows = _query(client, "viewer", sql).json()["rows"]
    assert len(rows) == 3 and rows[0][1] is None


# --- authorization ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,denied",
    [
        ("SELECT * FROM fact_generator_daily", "fact_generator_daily"),
        ("SELECT count(*) FROM fact_national_daily n JOIN fact_facility_daily f USING (period)",
         "fact_facility_daily"),
        ("SELECT * FROM fact_national_daily WHERE period IN (SELECT period FROM fact_generator_daily)",
         "fact_generator_daily"),
        ("WITH t AS (SELECT * FROM dim_generator) SELECT count(*) FROM t", "dim_generator"),
        ("SELECT period FROM fact_national_daily UNION SELECT period_start FROM dq_issue", "dq_issue"),
        ("SELECT (SELECT max(facility_name) FROM dim_facility)", "dim_facility"),
    ],
)  # fmt: skip
def test_viewer_is_refused_any_query_touching_a_detail_table(client, api_model, sql, denied):
    resp = _query(client, "viewer", sql)
    assert resp.status_code == 403
    assert resp.json()["error"] == {
        "code": "forbidden",
        "message": f"role 'viewer' has no access to: {denied}",
    }


def test_viewer_cannot_select_the_hidden_metric_column(client, api_model):
    # Not in the Viewer's serving file at all, so the engine itself cannot find it.
    resp = _query(client, "viewer", "SELECT missing_units FROM metric_fleet_offline_daily")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "query_error"
    star = _query(client, "viewer", "SELECT * FROM metric_fleet_offline_daily").json()
    assert "missing_units" not in [c["name"] for c in star["columns"]]
    assert "6462" not in str(star["rows"])


def test_a_cte_named_like_a_detail_table_reads_only_the_cte(client, api_model):
    # sqlguard says this reads only fact_national_daily; DuckDB must agree (same rows come back).
    sql = (
        "WITH fact_generator_daily AS (SELECT * FROM fact_national_daily) "
        "SELECT period, capacity_mw FROM fact_generator_daily ORDER BY 1"
    )
    body = _query(client, "viewer", sql).json()
    assert body["tables"] == ["fact_national_daily"]
    assert (
        body["rows"]
        == _query(client, "viewer", "SELECT period, capacity_mw FROM fact_national_daily ORDER BY 1").json()[
            "rows"
        ]
    )


def test_query_requires_login(client, api_model):
    assert client.post("/api/query", json={"sql": "SELECT 1"}).status_code == 401


def test_authorization_is_decided_before_the_model_is_read(client):
    # No model built: a forbidden query is refused (403), a bad one rejected (400), neither reaches data.
    assert _query(client, "viewer", "SELECT * FROM fact_generator_daily").status_code == 403
    assert _query(client, "analyst", "DELETE FROM fact_national_daily").status_code == 400
    assert _query(client, "analyst", "SELECT * FROM fact_national_daily").status_code == 503


# --- read-only ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("user", ["analyst", "admin", "viewer"])
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fact_national_daily",
        "UPDATE fact_national_daily SET outage_mw = 0",
        "INSERT INTO fact_national_daily SELECT * FROM fact_national_daily",
        "DROP TABLE fact_national_daily",
        "CREATE TABLE copy AS SELECT * FROM fact_national_daily",
        "SELECT 1; DELETE FROM fact_national_daily",
        "COPY fact_national_daily TO 'stolen.csv'",
        "ATTACH 'other.duckdb' AS other",
        "SET enable_external_access = true",
        "SELECT * FROM read_parquet('../data/model/builds/*/fact_generator_daily/*/*.parquet')",
    ],
)
def test_write_and_escape_statements_are_rejected_for_every_role(client, api_model, user, sql):
    resp = _query(client, user, sql)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] in {"read_only", "one_statement_only", "function_not_allowed"}
    assert _query(client, user, "SELECT count(*) FROM fact_national_daily").json()["rows"] == [[3]]


def test_unknown_table_is_400(client, api_model):
    resp = _query(client, "analyst", "SELECT * FROM users")
    assert (resp.status_code, resp.json()["error"]["code"]) == (400, "unknown_table")


def test_engine_errors_come_back_as_400(client, api_model):
    resp = _query(client, "analyst", "SELECT no_such_column FROM fact_national_daily")
    assert resp.status_code == 400
    assert "no_such_column" in resp.json()["error"]["message"]


# --- limits ----------------------------------------------------------------------------------------


def test_rows_are_capped_and_the_cut_is_reported(client, api_model):
    body = _query(
        client, "analyst", "SELECT * FROM fact_generator_daily ORDER BY period DESC", max_rows=2
    ).json()
    assert body["row_count"] == 2 and body["truncated"] is True
    assert {r[0] for r in body["rows"]} == {"2015-10-03"}  # the cap keeps the query's order


def test_a_smaller_limit_in_the_query_is_kept(client, api_model):
    body = _query(client, "analyst", "SELECT * FROM fact_generator_daily LIMIT 3", max_rows=5).json()
    assert body["row_count"] == 3 and body["truncated"] is False


def test_exactly_max_rows_is_not_truncated(client, api_model):
    body = _query(client, "analyst", "SELECT * FROM fact_generator_daily", max_rows=8).json()
    assert body["row_count"] == 8 and body["truncated"] is False


@pytest.mark.parametrize("max_rows", [0, 5001])
def test_max_rows_is_bounded(client, api_model, max_rows):
    assert _query(client, "analyst", "SELECT 1", max_rows=max_rows).status_code == 422


def test_long_queries_are_interrupted(api_settings, api_model):
    settings = api_settings.model_copy(update={"query_timeout_seconds": 0.3})
    with TestClient(create_app(settings)) as c:
        sql = (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 1000000000) "
            "SELECT count(*) FROM r"
        )
        resp = _query(c, "analyst", sql)
    assert resp.status_code == 408
    assert resp.json()["error"]["code"] == "query_timeout"


def test_the_cap_is_applied_by_the_engine_not_after_fetching(client, api_model, monkeypatch):
    sent = []
    real_run = query_module.run
    monkeypatch.setattr(
        query_module, "run", lambda con, sql, *limits: (sent.append(sql), real_run(con, sql, *limits))[1]
    )
    _query(client, "analyst", "SELECT * FROM fact_generator_daily", max_rows=2)
    assert sent == ["SELECT * FROM fact_generator_daily LIMIT 3"]  # one extra row detects the cut


# --- paging past max_rows --------------------------------------------------------------------------

ORDERED = "SELECT period, facility_id, generator_id FROM fact_generator_daily ORDER BY 1 DESC, 2, 3"


def test_next_offset_walks_every_row_exactly_once(client, api_model):
    everything = _query(client, "analyst", ORDERED).json()["rows"]
    pages, offset = [], 0
    while offset is not None:
        assert len(pages) < 10, "paging does not terminate"
        body = _query(client, "analyst", ORDERED, max_rows=3, offset=offset).json()
        assert body["offset"] == offset and body["ordered"] is True
        pages.append(body["rows"])
        offset = body["next_offset"]
    assert [len(p) for p in pages] == [3, 3, 2]
    assert [r for p in pages for r in p] == everything


def test_paging_stays_inside_the_query_own_limit(client, api_model):
    body = _query(client, "analyst", ORDERED + " LIMIT 4", max_rows=3, offset=3).json()
    assert body["row_count"] == 1 and body["truncated"] is False and body["next_offset"] is None


def test_an_offset_past_the_end_is_an_empty_last_page(client, api_model):
    body = _query(client, "analyst", ORDERED, offset=100).json()
    assert body["rows"] == [] and body["next_offset"] is None


def test_unordered_queries_are_flagged(client, api_model):
    assert _query(client, "analyst", "SELECT * FROM fact_national_daily").json()["ordered"] is False


def test_percent_limit_is_paged_from_outside(client, api_model):
    body = _query(client, "analyst", ORDERED + " LIMIT 50 PERCENT", max_rows=2, offset=2).json()
    assert body["row_count"] == 2 and body["truncated"] is False  # 50% of 8 rows = 4


def test_offset_is_bounded(client, api_model):
    assert _query(client, "analyst", "SELECT 1", offset=-1).status_code == 422


# --- result size -----------------------------------------------------------------------------------


def test_a_result_over_the_size_cap_is_refused(api_settings, api_model):
    settings = api_settings.model_copy(update={"query_max_result_bytes": 500})
    padding = "'" + "x" * 100 + "'"
    with TestClient(create_app(settings)) as c:
        small = _query(c, "analyst", "SELECT facility_id FROM fact_generator_daily")  # 8 rows, ~100 bytes
        big = _query(c, "analyst", f"SELECT facility_id, {padding} FROM fact_generator_daily")  # ~900 bytes
    assert small.status_code == 200 and small.json()["row_count"] == 8
    assert big.status_code == 400
    assert big.json()["error"] == {
        "code": "result_too_large",
        "message": "the result is larger than 0.0005 MB; select fewer columns or rows",
    }


def test_approx_size_counts_text_and_nested_values():
    assert query_module.approx_size("abc") == 3
    assert query_module.approx_size(1.5) == 8
    assert query_module.approx_size(("ab", ["cd", None])) == 8 + 2 + (8 + 2 + 8)
    assert query_module.approx_size({"k": "vv"}) == 8 + 1 + 2
