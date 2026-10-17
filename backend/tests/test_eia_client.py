"""EIA client tests against responses recorded from the real API (tests/fixtures/eia)."""

import logging
from datetime import date

import httpx
import pytest
import respx

from outage_explorer.connector import eia_client
from outage_explorer.connector.eia_client import (
    EIAAuthError,
    EIABadResponseError,
    EIAClient,
    EIARequestError,
    EIATransientError,
)
from outage_explorer.connector.routes import FACILITY, NATIONAL

BASE = "https://api.test/v2/nuclear-outages"
KEY = "SECRET-TEST-KEY"
D1, D2 = date(2026, 10, 1), date(2026, 10, 3)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    c = EIAClient(KEY, BASE, max_retries=3, sleep=sleeps.append)
    yield c
    c.close()


def page(rows):
    return {"response": {"total": "999", "data": rows}}


@respx.mock
def test_single_short_page_returns_rows(client, fixture_json):
    respx.get(f"{BASE}/us-nuclear-outages/data/").respond(json=fixture_json("national_3days.json"))
    pages = list(client.iter_pages(NATIONAL, D1, D2))
    assert len(pages) == 1
    assert [r["period"] for r in pages[0]] == ["2026-10-03", "2026-10-02", "2026-10-01"]


@respx.mock
def test_paginates_until_short_page_ignoring_total(client, monkeypatch):
    # The facility route's `total` is wrong in production; we must stop on a short page, not on total.
    monkeypatch.setattr(eia_client, "PAGE_SIZE", 2)
    route = respx.get(f"{BASE}/facility-nuclear-outages/data/").mock(
        side_effect=[
            httpx.Response(200, json=page([{"n": 1}, {"n": 2}])),
            httpx.Response(200, json=page([{"n": 3}, {"n": 4}])),
            httpx.Response(200, json=page([{"n": 5}])),
        ]
    )
    rows = [r for p in client.iter_pages(FACILITY, D1, D2) for r in p]
    assert [r["n"] for r in rows] == [1, 2, 3, 4, 5]
    offsets = [call.request.url.params["offset"] for call in route.calls]
    assert offsets == ["0", "2", "4"]


@respx.mock
def test_exactly_full_last_page_then_empty_page_stops(client, monkeypatch):
    monkeypatch.setattr(eia_client, "PAGE_SIZE", 2)
    respx.get(f"{BASE}/us-nuclear-outages/data/").mock(
        side_effect=[httpx.Response(200, json=page([{"n": 1}, {"n": 2}])), httpx.Response(200, json=page([]))]
    )
    assert [len(p) for p in client.iter_pages(NATIONAL, D1, D2)] == [2]


@respx.mock
def test_request_sorts_by_natural_key_for_stable_pagination(client, fixture_json):
    route = respx.get(f"{BASE}/facility-nuclear-outages/data/").respond(
        json=fixture_json("facility_sample.json")
    )
    client.fetch_page(FACILITY, D1, D2, 0)
    params = route.calls[0].request.url.params
    assert params["sort[0][column]"] == "period"
    assert params["sort[1][column]"] == "facility"
    assert params["api_key"] == KEY


@respx.mock
@pytest.mark.parametrize("fixture", ["error_api_key_invalid.json", "error_api_key_missing.json"])
def test_bad_key_is_fatal_and_not_retried(client, fixture_json, fixture, sleeps):
    route = respx.get(f"{BASE}/us-nuclear-outages/data/").respond(403, json=fixture_json(fixture))
    with pytest.raises(EIAAuthError, match="API_KEY_"):
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert route.call_count == 1
    assert sleeps == []


def test_empty_key_fails_before_any_request():
    with pytest.raises(EIAAuthError):
        EIAClient("", BASE)


@respx.mock
def test_gateway_timeout_html_is_retried_then_succeeds(client, fixture_text, fixture_json, sleeps):
    route = respx.get(f"{BASE}/us-nuclear-outages/data/").mock(
        side_effect=[
            httpx.Response(
                504, text=fixture_text("gateway_timeout_504.html"), headers={"Content-Type": "text/html"}
            ),
            httpx.ReadTimeout("timed out"),
            httpx.Response(200, json=fixture_json("national_3days.json")),
        ]
    )
    rows = client.fetch_page(NATIONAL, D1, D2, 0)
    assert len(rows) == 3
    assert route.call_count == 3
    assert len(sleeps) == 2


@respx.mock
def test_transient_errors_give_up_after_max_retries(client, sleeps):
    route = respx.get(f"{BASE}/us-nuclear-outages/data/").respond(503)
    with pytest.raises(EIATransientError):
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert route.call_count == 4  # 1 try + 3 retries
    assert len(sleeps) == 3


@respx.mock
def test_429_honours_retry_after(client, fixture_json, sleeps):
    respx.get(f"{BASE}/us-nuclear-outages/data/").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=fixture_json("national_3days.json")),
        ]
    )
    client.fetch_page(NATIONAL, D1, D2, 0)
    assert sleeps == [7.0]


@respx.mock
def test_200_with_html_body_is_treated_as_transient(client, fixture_json):
    respx.get(f"{BASE}/us-nuclear-outages/data/").mock(
        side_effect=[
            httpx.Response(200, text="<html>busy</html>"),
            httpx.Response(200, json=fixture_json("national_3days.json")),
        ]
    )
    assert len(client.fetch_page(NATIONAL, D1, D2, 0)) == 3


@respx.mock
def test_unexpected_json_shape_is_fatal(client, sleeps):
    respx.get(f"{BASE}/us-nuclear-outages/data/").respond(json={"response": {"total": "0"}})
    with pytest.raises(EIABadResponseError):
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert sleeps == []


@respx.mock
def test_other_4xx_is_fatal(client):
    respx.get(f"{BASE}/us-nuclear-outages/data/").respond(400, json={"error": "invalid column"})
    with pytest.raises(EIARequestError):
        client.fetch_page(NATIONAL, D1, D2, 0)


@respx.mock
def test_api_key_never_appears_in_errors_or_logs(client, caplog):
    respx.get(f"{BASE}/us-nuclear-outages/data/").mock(side_effect=httpx.ConnectError("boom"))
    with caplog.at_level(logging.DEBUG), pytest.raises(EIATransientError) as excinfo:
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert KEY not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__
    assert KEY not in caplog.text


@respx.mock
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), httpx.ConnectError("boom")])
def test_failure_exceptions_do_not_chain_the_httpx_error(client, failure):
    # httpx exceptions hold the request, whose URL contains api_key; a chained traceback would print it.
    respx.get(f"{BASE}/us-nuclear-outages/data/").mock(side_effect=failure)
    with pytest.raises(EIATransientError) as excinfo:
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


@respx.mock
def test_httpx_request_log_does_not_leak_key(client, fixture_json, caplog):
    respx.get(f"{BASE}/us-nuclear-outages/data/").respond(json=fixture_json("national_3days.json"))
    with caplog.at_level(logging.DEBUG):
        client.fetch_page(NATIONAL, D1, D2, 0)
    assert KEY not in caplog.text
