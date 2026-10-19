"""sqlguard: what SQL is accepted, and which tables it is found to read."""

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from outage_explorer.api import sqlguard
from outage_explorer.api.sqlguard import SqlRejected, check, with_page
from outage_explorer.config import Settings
from outage_explorer.model import explore
from outage_explorer.model.build import build_model
from outage_explorer.model.tables import TABLES
from tests.conftest import fleet_rows

KNOWN = set(TABLES)
FINDINGS_SQL = Path(__file__).resolve().parents[2] / "analysis" / "findings.sql"


def tables(sql: str) -> set[str]:
    return set(check(sql, KNOWN).tables)


def rejected(sql: str) -> str:
    with pytest.raises(SqlRejected) as info:
        check(sql, KNOWN)
    return info.value.code


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM fact_national_daily", {"fact_national_daily"}),
        ("FROM fact_national_daily", {"fact_national_daily"}),  # DuckDB's FROM-first form
        ('select * from FACT_National_Daily n join "dim_facility" f on true',
         {"fact_national_daily", "dim_facility"}),
        ("SELECT (SELECT max(period) FROM fact_facility_daily) FROM fact_national_daily",
         {"fact_facility_daily", "fact_national_daily"}),
        ("SELECT * FROM fact_national_daily WHERE period IN (SELECT period FROM fact_generator_daily)",
         {"fact_national_daily", "fact_generator_daily"}),
        ("SELECT * FROM fact_national_daily WHERE EXISTS (SELECT 1 FROM dq_issue)",
         {"fact_national_daily", "dq_issue"}),
        ("SELECT period FROM fact_national_daily UNION ALL SELECT period FROM dim_generator ORDER BY 1",
         {"fact_national_daily", "dim_generator"}),
        ("SELECT * FROM fact_national_daily, LATERAL (SELECT * FROM dim_facility) d",
         {"fact_national_daily", "dim_facility"}),
        ("SELECT 1 AS x", set()),
    ],
)  # fmt: skip
def test_tables_are_found_wherever_they_appear(sql, expected):
    assert tables(sql) == expected


def test_a_cte_hides_nothing_it_reads():
    assert tables("WITH t AS (SELECT * FROM fact_generator_daily) SELECT * FROM t") == {
        "fact_generator_daily"
    }


def test_ctes_see_only_earlier_ctes():
    assert tables("WITH b AS (SELECT 1), a AS (SELECT * FROM b) SELECT * FROM a") == set()
    assert rejected("WITH a AS (SELECT * FROM b), b AS (SELECT 1) SELECT * FROM a") == "unknown_table"


def test_recursive_cte_may_reference_itself():
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT * FROM r"
    assert tables(sql) == set()


def test_a_cte_named_like_a_table_does_not_hide_that_table_elsewhere():
    # The CTE lives inside the subquery; the second fact_generator_daily is the real table.
    sql = (
        "SELECT * FROM (WITH fact_generator_daily AS (SELECT 1) SELECT * FROM fact_generator_daily) a, "
        "fact_generator_daily"
    )
    assert tables(sql) == {"fact_generator_daily"}
    # Inside a non-recursive CTE's own body, its name still means the table.
    inner = (
        "WITH fact_generator_daily AS (SELECT * FROM fact_generator_daily) SELECT * FROM fact_generator_daily"
    )
    assert tables(inner) == {"fact_generator_daily"}


@pytest.mark.parametrize(
    "sql,code",
    [
        ("INSERT INTO dq_issue SELECT * FROM dq_issue", "read_only"),
        ("UPDATE fact_national_daily SET outage_mw = 0", "read_only"),
        ("DELETE FROM fact_national_daily", "read_only"),
        ("DROP TABLE fact_national_daily", "read_only"),
        ("CREATE TABLE x AS SELECT * FROM fact_national_daily", "read_only"),
        ("CREATE VIEW v AS SELECT 1", "read_only"),
        ("ALTER TABLE fact_national_daily ADD COLUMN x INT", "read_only"),
        ("COPY fact_national_daily TO 'out.csv'", "read_only"),
        ("ATTACH 'other.db' AS other", "read_only"),
        ("SET enable_external_access = true", "read_only"),
        ("PRAGMA database_list", "read_only"),
        ("DESCRIBE fact_national_daily", "read_only"),
        ("SUMMARIZE fact_national_daily", "read_only"),
        ("EXPLAIN SELECT 1", "read_only"),
        ("INSTALL httpfs", "read_only"),
        ("SELECT * INTO t FROM fact_national_daily", "read_only"),
        ("SELECT 1; DELETE FROM fact_national_daily", "one_statement_only"),
        ("SELECT 1; SELECT 2", "one_statement_only"),
        ("", "one_statement_only"),
        ("SELECT * FROM read_parquet('data/model/**/*.parquet')", "function_not_allowed"),
        ("SELECT * FROM read_csv_auto('C:/Windows/win.ini')", "function_not_allowed"),
        ("SELECT read_text('secret.txt')", "function_not_allowed"),
        ("SELECT * FROM glob('*')", "function_not_allowed"),
        ("SELECT * FROM duckdb_tables()", "function_not_allowed"),
        ("SELECT * FROM query_table('fact_generator_daily')", "function_not_allowed"),
        ("SELECT getenv('JWT_SECRET')", "function_not_allowed"),
        ("SELECT * FROM range(10)", "function_not_allowed"),  # caught by the allow-list first
        ("SELECT * FROM main.fact_generator_daily", "source_not_allowed"),
        ("SELECT * FROM information_schema.tables", "source_not_allowed"),
        ("SELECT * FROM 'data/raw/generator/year=2015/data.parquet'", "unknown_table"),
        ("SELECT * FROM raw_generator", "unknown_table"),
        ("SELEC 1", "syntax_error"),
    ],
)
def test_rejected_statements(sql, code):
    assert rejected(sql) == code


# Each of these built a value of ~1 GB or more past DuckDB's memory limit in the security review,
# and most held the query far past its timeout (DECISIONS #27).
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT repeat('x', 2000000000)",
        "SELECT length(repeat(repeat('x', 40000), 25000))",
        "SELECT lpad('', 1000000000, 'x')",
        "SELECT rpad('', 1000000000, 'x')",
        "SELECT space(1000000000)",
        "SELECT printf('%01000000000d', 1)",
        "SELECT format('{:>1000000000}', 1)",
        "SELECT replace(replace(replace('x', 'x', 'xxxx'), 'x', 'xxxx'), 'x', 'xxxx')",
        "SELECT regexp_replace('x', 'x', 'xxxx', 'g')",
        "SELECT len(range(1000000000))",
        "SELECT len(generate_series(1, 1000000000))",
        "SELECT len(list_resize([1], 1000000000))",
        "SELECT array_to_string(list(period), 'x') FROM fact_national_daily",
        "SELECT list_reduce([1, 2], (a, x) -> a || a)",
        "SELECT current_setting('memory_limit')",
        "SELECT version()",
    ],
)
def test_functions_outside_the_allow_list_are_rejected(sql):
    assert rejected(sql) == "function_not_allowed"


@pytest.mark.parametrize(
    "expression",
    [
        "strftime(period, '%Y-%m')", "date_trunc('month', period)", "date_diff('day', period, today())",
        "date_part('year', period)", "extract(year FROM period)", "isodow(period)", "period + INTERVAL 1 DAY",
        "string_agg(facility_id, ',')", "list(facility_id)", "count(DISTINCT facility_id)",
        "sum(outage_mw) FILTER (WHERE outage_mw > 0)", "quantile_cont(outage_mw, 0.5)", "median(outage_mw)",
        "arg_max(facility_id, outage_mw)", "round(avg(outage_mw), 2)", "coalesce(outage_mw, 0)",
        "CASE WHEN outage_mw > 0 THEN 'out' ELSE 'up' END", "outage_mw::INT", "TRY_CAST(facility_id AS INT)",
        "lower(facility_name)", "split_part(facility_id, '/', 1)", "facility_name ILIKE '%river%'",
        "regexp_matches(facility_name, 'Bend')", "concat(facility_id, '/', generator_id)",
        "lag(outage_mw) OVER (ORDER BY period)", "row_number() OVER (PARTITION BY facility_id)",
        "greatest(outage_mw, 0)", "unnest([1, 2])", "len([1, 2])",
    ],
)  # fmt: skip
def test_common_analysis_functions_are_allowed(expression):
    sql = f"SELECT {expression} FROM fact_generator_daily GROUP BY ALL"
    assert tables(sql) == {"fact_generator_daily"}


def test_overlong_queries_are_rejected():
    assert rejected("SELECT 1" + " " * sqlguard.MAX_SQL_LENGTH) == "query_too_long"


@pytest.mark.parametrize(
    "sql,offset,expected",
    [
        ("SELECT * FROM dim_facility", 0, "SELECT * FROM dim_facility LIMIT 11"),
        ("SELECT * FROM dim_facility", 20, "SELECT * FROM dim_facility LIMIT 11 OFFSET 20"),
        ("SELECT * FROM dim_facility LIMIT 3", 0, "SELECT * FROM dim_facility LIMIT 3"),
        ("SELECT * FROM dim_facility LIMIT 500", 0, "SELECT * FROM dim_facility LIMIT 11"),
        ("SELECT * FROM dim_facility LIMIT 15", 10, "SELECT * FROM dim_facility LIMIT 5 OFFSET 10"),
        ("SELECT * FROM dim_facility LIMIT 15", 30, "SELECT * FROM dim_facility LIMIT 0 OFFSET 30"),
        ("SELECT * FROM dim_facility OFFSET 5", 0, "SELECT * FROM dim_facility LIMIT 11 OFFSET 5"),
        ("SELECT * FROM dim_facility LIMIT 20 OFFSET 5", 10, "SELECT * FROM dim_facility LIMIT 10 OFFSET 15"),
        ("SELECT 1 UNION ALL SELECT 2 ORDER BY 1 DESC", 1,
         "SELECT 1 UNION ALL SELECT 2 ORDER BY 1 DESC LIMIT 11 OFFSET 1"),
        ("SELECT * FROM dim_facility LIMIT (SELECT 2)", 0,
         "SELECT * FROM (SELECT * FROM dim_facility LIMIT (SELECT 2)) AS paged LIMIT 11"),
        ("SELECT * FROM dim_facility LIMIT 10 PERCENT", 4,
         "SELECT * FROM (SELECT * FROM dim_facility LIMIT 10 PERCENT) AS paged LIMIT 11 OFFSET 4"),
    ],
)  # fmt: skip
def test_page_is_merged_into_the_query_limit_and_offset(sql, offset, expected):
    assert with_page(check(sql, KNOWN), offset, 11).sql() == expected


def test_pages_of_a_limited_ordered_query_match_slices_of_its_result(model_con):
    sql = (
        "SELECT period, facility_id, generator_id FROM fact_generator_daily "
        "ORDER BY 1 DESC, 2, 3 LIMIT 3 OFFSET 1"
    )
    full = model_con.sql(sql).fetchall()
    checked = check(sql, KNOWN)
    pages = [model_con.sql(with_page(checked, o, 2).sql()).fetchall() for o in (0, 2, 4, 6)]
    assert len(full) == 3
    assert pages == [full[0:2], full[2:3], [], []]


@pytest.fixture
def model_con(tmp_path, write_fleet):
    settings = Settings(data_dir=tmp_path)
    days = [date(2015, 10, 1) + timedelta(days=i) for i in range(3)]
    write_fleet(
        fleet_rows({("6462", "1"): 967.0, ("46", "1"): 1000.0}, days, missing={(days[1], "6462", "1")})
    )
    build_model(settings)
    con = duckdb.connect()
    explore.create_views(con, settings.data_path)
    return con


def test_regenerated_sql_returns_what_the_original_returns(model_con):
    """What runs is sqlglot's regeneration of the checked tree. On every FINDINGS.md query over model
    tables, it must give the same result as the text as written (no parser differences that matter)."""
    compared = 0
    for name, sql in explore.blocks(FINDINGS_SQL.read_text(encoding="utf-8")):
        try:
            checked = check(sql, KNOWN)
        except SqlRejected:
            continue  # reads raw_* tables, which the API does not serve
        assert model_con.sql(checked.sql()).fetchall() == model_con.sql(sql).fetchall(), name
        compared += 1
    assert compared >= 10
