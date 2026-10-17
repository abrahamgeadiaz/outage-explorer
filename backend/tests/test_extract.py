"""End-to-end extraction runs against a fake EIA API (respx), writing real Parquet to a temp dir."""

import json
from datetime import date, timedelta

import duckdb
import httpx
import pytest
import respx
from pydantic import SecretStr

from outage_explorer.config import Settings
from outage_explorer.connector import cli
from outage_explorer.connector import extract as extract_mod
from outage_explorer.connector.eia_client import EIAClient
from outage_explorer.connector.extract import ExtractionLockedError, run_extraction

BASE = "https://api.test/v2/nuclear-outages"
KEY = "SECRET-TEST-KEY"
START = date(2025, 12, 28)
TODAY = date(2026, 1, 3)  # window crosses a year boundary -> two chunks per dataset
UNITS = {"capacity-units": "megawatts", "outage-units": "megawatts", "percentOutage-units": "percent"}


def fake_rows(path: str, start: date, end: date, overrides: dict) -> list[dict]:
    rows, day = [], start
    while day <= end:
        p = day.isoformat()
        if "us-nuclear" in path:
            rows.append({"period": p, "capacity": "3000", "outage": "100", "percentOutage": "3.33", **UNITS})
        for fac in ("46", "204"):
            if "facility-nuclear" in path:
                rows.append(
                    {
                        "period": p,
                        "facility": fac,
                        "facilityName": f"F{fac}",
                        "capacity": "1500",
                        "outage": "50",
                        "percentOutage": "3.33",
                        **UNITS,
                    }
                )
            for gen in ("1", "2"):
                if "generator-nuclear" in path:
                    rows.append(
                        {
                            "period": p,
                            "facility": fac,
                            "facilityName": f"F{fac}",
                            "generator": gen,
                            "capacity": "750",
                            "outage": "25",
                            "percentOutage": "3.33",
                            **UNITS,
                        }
                    )
        day += timedelta(days=1)
    for r in rows:
        r.update(overrides.get((path.split("/")[-3], r["period"]), {}))
    return rows


class FakeEIA:
    """Serves generated rows, paginated like the real API, with hooks to inject failures."""

    def __init__(self):
        self.overrides: dict = {}  # (route, period) -> field overrides
        self.fail: callable = lambda request: None  # return an httpx.Response to fail a request

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (failure := self.fail(request)) is not None:
            return failure
        p = request.url.params
        rows = fake_rows(
            request.url.path, date.fromisoformat(p["start"]), date.fromisoformat(p["end"]), self.overrides
        )
        offset, length = int(p["offset"]), int(p["length"])
        return httpx.Response(
            200, json={"response": {"total": str(len(rows)), "data": rows[offset : offset + length]}}
        )


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr("outage_explorer.connector.eia_client.PAGE_SIZE", 7)  # force several pages
    f = FakeEIA()
    with respx.mock:
        respx.get(url__startswith=BASE).mock(side_effect=f)
        yield f


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        eia_api_key=KEY,
        eia_base_url=BASE,
        data_dir=tmp_path,
        eia_start_date=START,
        eia_max_concurrency=2,
        refresh_lookback_days=3,
    )


def run(settings, **kw):
    client = EIAClient(KEY, BASE, max_retries=1, sleep=lambda s: None)
    return run_extraction(settings, client=client, today=kw.pop("today", TODAY), **kw)


def count(settings, dataset, distinct_keys=False):
    keys = {"national": "period", "facility": "period, facility", "generator": "period, facility, generator"}
    expr = f"count(DISTINCT ({keys[dataset]}))" if distinct_keys else "count(*)"
    glob = (settings.data_path / "raw" / dataset / "*" / "*.parquet").as_posix()
    return duckdb.sql(f"SELECT {expr} FROM read_parquet('{glob}')").fetchone()[0]


DAYS = (TODAY - START).days + 1


def test_full_run_writes_all_datasets_by_year(fake, settings):
    summary = run(settings)
    assert summary["status"] == "success"
    assert count(settings, "national") == DAYS
    assert count(settings, "facility") == DAYS * 2
    assert count(settings, "generator") == DAYS * 4
    years = sorted(p.name for p in (settings.data_path / "raw" / "generator").iterdir())
    assert years == ["year=2025", "year=2026"]
    assert summary["datasets"]["generator"]["watermark"] == TODAY.isoformat()


def test_two_runs_do_not_duplicate_or_change_data(fake, settings):
    run(settings)
    second = run(settings, full=True)
    for name in ("national", "facility", "generator"):
        assert count(settings, name) == count(settings, name, distinct_keys=True)
        assert second["datasets"][name]["inserted"] == 0
        assert second["datasets"][name]["updated"] == 0
    assert count(settings, "generator") == DAYS * 4


def test_incremental_run_starts_from_watermark_minus_lookback(fake, settings):
    run(settings)
    later = TODAY + timedelta(days=2)
    summary = run(settings, today=later)
    assert summary["datasets"]["national"]["window"] == [
        (TODAY - timedelta(days=3)).isoformat(),
        later.isoformat(),
    ]
    assert summary["datasets"]["national"]["inserted"] == 2
    assert count(settings, "national") == DAYS + 2


def test_revision_inside_lookback_is_applied_and_counted(fake, settings):
    run(settings)
    fake.overrides[("us-nuclear-outages", "2026-01-02")] = {"outage": "999"}
    summary = run(settings)
    assert summary["datasets"]["national"]["updated"] == 1
    glob = (settings.data_path / "raw" / "national" / "*" / "*.parquet").as_posix()
    value = duckdb.sql(
        f"SELECT outage FROM read_parquet('{glob}') WHERE period = DATE '2026-01-02'"
    ).fetchone()
    assert value == (999.0,)


def test_bad_key_fails_run_and_writes_no_data(fake, settings):
    fake.fail = lambda req: httpx.Response(403, json={"error": {"code": "API_KEY_INVALID", "message": "x"}})
    summary = run(settings)
    assert summary["status"] == "failed"
    assert "API_KEY_INVALID" in summary["error"]
    assert not (settings.data_path / "raw").exists()
    assert extract_mod.load_state(settings.data_path) == {}


def test_persistent_failure_in_one_chunk_is_partial_and_holds_watermark(fake, settings):
    def fail_generator_2025(req):
        if "generator" in req.url.path and req.url.params["start"].startswith("2025"):
            return httpx.Response(503)
        return None

    fake.fail = fail_generator_2025
    summary = run(settings)
    assert summary["status"] == "partial"
    gen = summary["datasets"]["generator"]
    assert gen["status"] == "partial"
    assert gen["watermark"] is None  # never advanced: the next run retries the failed window
    assert [c["ok"] for c in gen["chunks"]] == [False, True]
    assert summary["datasets"]["national"]["status"] == "success"

    fake.fail = lambda req: None
    assert run(settings)["status"] == "success"
    assert count(settings, "generator") == DAYS * 4


def test_all_rows_invalid_is_a_chunk_failure_not_an_empty_write(fake, settings):
    run(settings)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        fake.overrides[("us-nuclear-outages", d)] = {"capacity": "oops"}
    summary = run(settings, datasets=["national"], start=date(2026, 1, 1))
    assert summary["datasets"]["national"]["status"] == "failed"
    assert summary["datasets"]["national"]["quality"]["rejected_by_reason"] == {"invalid_capacity": 3}
    assert count(settings, "national") == DAYS  # stored data untouched


def test_run_metadata_is_persisted_without_the_key(fake, settings):
    summary = run(settings)
    path = settings.data_path / "_meta" / "runs" / f"{summary['run_id']}.json"
    text = path.read_text(encoding="utf-8")
    assert json.loads(text)["status"] == "success"
    assert KEY not in text
    assert not (settings.data_path / "_meta" / "extract.lock").exists()


def test_concurrent_run_is_refused(fake, settings):
    lock = settings.data_path / "_meta" / "extract.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("other-run", encoding="utf-8")
    with pytest.raises(ExtractionLockedError):
        run(settings)
    assert lock.read_text(encoding="utf-8") == "other-run"  # we did not steal someone else's lock


def test_cli_without_key_exits_2_before_any_request(monkeypatch, settings):
    monkeypatch.setattr(
        cli, "get_settings", lambda: settings.model_copy(update={"eia_api_key": SecretStr("")})
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE)
        assert cli.main([]) == 2
        assert route.call_count == 0


def test_all_rows_invalid_on_first_load_is_failure_not_empty_success(fake, settings):
    # No stored data to protect here, so only the "every row rejected" check stops a silent empty success.
    fake.overrides.update(
        {("us-nuclear-outages", d): {"capacity": "oops"} for d in ("2026-01-01", "2026-01-02")}
    )
    summary = run(settings, datasets=["national"], start=date(2026, 1, 1), end=date(2026, 1, 2))
    assert summary["status"] == "failed"
    assert summary["datasets"]["national"]["watermark"] is None
