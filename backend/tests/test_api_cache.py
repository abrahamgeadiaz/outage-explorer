"""Query result cache: hits, expiry, refresh invalidation, and never crossing a permission boundary."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from outage_explorer.api import query as query_module
from outage_explorer.api import refresh
from outage_explorer.api.app import create_app
from outage_explorer.api.cache import QueryCache
from outage_explorer.model.build import build_model
from tests.conftest import API_UNITS, auth_headers, fleet_rows
from tests.test_api_refresh import _summary, _wait

NATIONAL = "SELECT period, capacity_mw FROM fact_national_daily ORDER BY period"


@pytest.fixture
def engine_calls(monkeypatch):
    """Every SQL string that actually reached DuckDB."""
    sent = []
    real_run = query_module.run

    def spy(con, sql, timeout):
        sent.append(sql)
        return real_run(con, sql, timeout)

    monkeypatch.setattr(query_module, "run", spy)
    return sent


def _query(client, user, sql, **extra):
    return client.post("/api/query", json={"sql": sql, **extra}, headers=auth_headers(client, user))


def test_a_repeated_query_is_served_from_the_cache(client, api_model, engine_calls):
    first = _query(client, "analyst", NATIONAL).json()
    second = _query(client, "analyst", NATIONAL).json()
    assert (first["cached"], second["cached"]) == (False, True)
    assert second["rows"] == first["rows"] and second["build_id"] == first["build_id"]
    assert len(engine_calls) == 1


def test_whitespace_and_keyword_case_do_not_matter(client, api_model, engine_calls):
    _query(client, "analyst", NATIONAL)
    again = _query(
        client, "analyst", "select   period, capacity_mw\nfrom fact_national_daily order by period"
    )
    assert again.json()["cached"] is True
    assert len(engine_calls) == 1


def test_each_page_is_its_own_entry(client, api_model, engine_calls):
    _query(client, "analyst", NATIONAL, max_rows=1)
    assert _query(client, "analyst", NATIONAL, max_rows=1, offset=1).json()["cached"] is False
    assert _query(client, "analyst", NATIONAL, max_rows=2).json()["cached"] is False
    assert len(engine_calls) == 3


def test_roles_with_different_grants_never_share_an_entry(client, api_model, engine_calls):
    _query(client, "analyst", NATIONAL)
    viewer = _query(client, "viewer", NATIONAL).json()
    assert viewer["cached"] is False  # same SQL, same data even, but a different grant
    assert len(engine_calls) == 2


def test_roles_with_the_same_grant_share_entries(client, api_model, engine_calls):
    _query(client, "analyst", NATIONAL)
    assert _query(client, "admin", NATIONAL).json()["cached"] is True
    assert len(engine_calls) == 1


def test_a_cached_result_is_no_way_around_authorization(client, api_model, engine_calls):
    sql = "SELECT count(*) FROM fact_generator_daily"
    assert _query(client, "analyst", sql).status_code == 200
    resp = _query(client, "viewer", sql)
    assert resp.status_code == 403  # checked before the cache is consulted
    assert "rows" not in resp.json()


def test_errors_are_not_cached(client, api_model, engine_calls):
    bad = "SELECT no_such_column FROM fact_national_daily"
    assert _query(client, "analyst", bad).status_code == 400
    assert _query(client, "analyst", bad).status_code == 400
    assert len(engine_calls) == 2


def test_entries_expire(api_settings, api_model, engine_calls):
    now = [1000.0]
    app = create_app(api_settings)
    app.state.query_cache = QueryCache(ttl_seconds=60, max_rows=1000, clock=lambda: now[0])
    with TestClient(app) as c:
        _query(c, "analyst", NATIONAL)
        now[0] += 59
        assert _query(c, "analyst", NATIONAL).json()["cached"] is True
        now[0] += 1
        assert _query(c, "analyst", NATIONAL).json()["cached"] is False
    assert len(engine_calls) == 2


def test_a_successful_refresh_clears_the_cache(client, api_model, engine_calls, monkeypatch, write_fleet):
    def extract(settings, full=False):
        write_fleet(fleet_rows(API_UNITS, [date(2015, 10, 4)]))
        return _summary()

    monkeypatch.setattr(refresh, "run_extraction", extract)
    _query(client, "analyst", NATIONAL)
    assert len(client.app.state.query_cache) == 1

    admin = auth_headers(client, "admin")
    _wait(client, admin, client.post("/api/refresh", headers=admin).json()["job_id"])
    assert len(client.app.state.query_cache) == 0
    after = _query(client, "analyst", NATIONAL).json()
    assert after["cached"] is False and len(after["rows"]) == 4  # the new day is there


def test_a_new_build_misses_even_without_a_refresh_job(client, api_settings, api_model, engine_calls):
    _query(client, "analyst", NATIONAL)
    build_model(api_settings)  # e.g. `uv run outage-model` from the CLI: the cache is not told
    assert _query(client, "analyst", NATIONAL).json()["cached"] is False


# --- the cache itself ------------------------------------------------------------------------------


def test_row_budget_evicts_least_recently_used():
    cache = QueryCache(ttl_seconds=60, max_rows=10)
    cache.put(("g", "b", "a"), "A", rows=4)
    cache.put(("g", "b", "b"), "B", rows=4)
    cache.get(("g", "b", "a"))  # a is now the most recently used
    cache.put(("g", "b", "c"), "C", rows=4)
    assert cache.get(("g", "b", "b")) is None
    assert cache.get(("g", "b", "a")) == "A" and cache.get(("g", "b", "c")) == "C"


def test_a_result_bigger_than_the_budget_is_not_stored():
    cache = QueryCache(ttl_seconds=60, max_rows=10)
    cache.put(("g", "b", "big"), "X", rows=11)
    assert len(cache) == 0


@pytest.mark.parametrize("ttl,max_rows", [(0, 100), (60, 0)])
def test_zero_disables_the_cache(ttl, max_rows):
    cache = QueryCache(ttl_seconds=ttl, max_rows=max_rows)
    cache.put(("g", "b", "q"), "A", rows=1)
    assert cache.get(("g", "b", "q")) is None
