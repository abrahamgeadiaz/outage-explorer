"""Refresh (US-06): Admin only, asynchronous, and the outcome is reported.

The EIA extraction is replaced by a fake that writes raw rows like the connector does (the connector
itself is tested against recorded responses in test_extract.py); the model build is the real one.
"""

import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from outage_explorer.api import refresh
from outage_explorer.api.app import create_app
from outage_explorer.connector.extract import ExtractionLockedError
from tests.conftest import API_UNITS, auth_headers, fleet_rows


def _summary(status="success", error=None, chunk_error=None) -> dict:
    datasets = {
        name: {
            "status": "partial" if chunk_error and name == "facility" else status,
            "rows_fetched": 3,
            "inserted": 1,
            "updated": 0,
            "watermark": "2015-10-04",
            "chunks": [
                {
                    "start": "2015-01-01",
                    "end": "2015-12-31",
                    "error": chunk_error if name == "facility" else None,
                }
            ],
        }
        for name in ("national", "facility", "generator")
    }
    out = {"run_id": "run-1", "status": status, "datasets": datasets}
    if error:
        out["error"] = error
    return out


@pytest.fixture
def fake_extract(monkeypatch, write_fleet):
    """Replace the EIA extraction: by default it adds one more day of data and succeeds."""
    calls = []

    def extract(settings, *, full=False, summary=None, before=None):
        calls.append({"full": full})
        if before:
            before()
        write_fleet(fleet_rows(API_UNITS, [date(2015, 10, 4)]))
        return summary or _summary()

    def install(**kwargs):
        monkeypatch.setattr(
            refresh, "run_extraction", lambda settings, full=False: extract(settings, full=full, **kwargs)
        )
        return calls

    return install


def _wait(client, headers, job_id, timeout=15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/refresh/{job_id}", headers=headers).json()
        if job["status"] in refresh.FINISHED:
            return job
        time.sleep(0.02)
    raise AssertionError(f"refresh {job_id} did not finish")


@pytest.mark.parametrize("user", ["viewer", "analyst"])
def test_only_admin_can_refresh_or_see_refresh_jobs(client, api_model, fake_extract, user):
    calls = fake_extract()
    headers = auth_headers(client, user)
    assert client.post("/api/refresh", headers=headers).status_code == 403
    assert client.get("/api/refresh", headers=headers).status_code == 403
    assert calls == []  # refused before anything ran


def test_refresh_requires_login(client):
    assert client.post("/api/refresh").status_code == 401


def test_admin_refresh_runs_in_the_background_and_reports_success(client, api_model, fake_extract):
    calls = fake_extract()
    headers = auth_headers(client, "admin")
    resp = client.post("/api/refresh", headers=headers)
    assert resp.status_code == 202
    started = resp.json()
    assert started["status"] == "running" and started["requested_by"] == "admin"

    job = _wait(client, headers, started["job_id"])
    assert job["status"] == "succeeded" and job["step"] == "done" and job["error"] is None
    assert job["datasets"]["generator"] == {
        "status": "success", "rows_fetched": 3, "inserted": 1, "updated": 0, "watermark": "2015-10-04"
    }  # fmt: skip
    assert job["build_id"] != api_model["build_id"]
    assert job["dq_issues"]["warning"] >= 1  # River Bend's gap is still in the data
    assert calls == [{"full": False}]

    # The new build is served at once, through every path.
    assert client.get("/api/health").json()["model_build"] == job["build_id"]
    rows = client.post(
        "/api/query", json={"sql": "SELECT max(period) FROM fact_national_daily"}, headers=headers
    )
    assert rows.json()["rows"] == [["2015-10-04"]]
    assert client.get("/api/refresh", headers=headers).json()[0]["job_id"] == job["job_id"]


def test_full_refresh_is_passed_to_the_connector(client, api_model, fake_extract):
    calls = fake_extract()
    headers = auth_headers(client, "admin")
    job = client.post("/api/refresh", json={"full": True}, headers=headers).json()
    _wait(client, headers, job["job_id"])
    assert calls == [{"full": True}]


def test_partial_extraction_still_rebuilds_and_says_what_failed(client, api_model, fake_extract):
    fake_extract(summary=_summary(status="partial", chunk_error="EIAServerError: HTTP 504"))
    headers = auth_headers(client, "admin")
    job = _wait(client, headers, client.post("/api/refresh", headers=headers).json()["job_id"])
    assert job["status"] == "partial"
    assert job["build_id"] is not None
    assert job["error"] == "facility 2015-01-01..2015-12-31: EIAServerError: HTTP 504"


def test_failed_extraction_rebuilds_nothing(client, api_model, fake_extract):
    fake_extract(summary=_summary(status="failed", error="EIA rejected the API key (API_KEY_INVALID)"))
    headers = auth_headers(client, "admin")
    job = _wait(client, headers, client.post("/api/refresh", headers=headers).json()["job_id"])
    assert job["status"] == "failed"
    assert (
        job["error"] == "extraction failed, nothing was rebuilt: EIA rejected the API key (API_KEY_INVALID)"
    )
    assert job["build_id"] is None
    assert client.get("/api/health").json()["model_build"] == api_model["build_id"]


def test_an_exception_in_the_job_is_reported_not_lost(client, api_model, monkeypatch):
    def locked(settings, full=False):
        raise ExtractionLockedError("another run is active (started by CLI)")

    monkeypatch.setattr(refresh, "run_extraction", locked)
    headers = auth_headers(client, "admin")
    job = _wait(client, headers, client.post("/api/refresh", headers=headers).json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"] == "ExtractionLockedError: another run is active (started by CLI)"


def test_only_one_refresh_runs_at_a_time(client, api_model, fake_extract):
    release = threading.Event()
    fake_extract(before=lambda: release.wait(10))
    headers = auth_headers(client, "admin")
    first = client.post("/api/refresh", headers=headers).json()
    second = client.post("/api/refresh", headers=headers)
    assert second.status_code == 409
    assert second.json()["error"] == {
        "code": "refresh_in_progress",
        "message": f"refresh {first['job_id']} is still running",
    }
    release.set()
    assert _wait(client, headers, first["job_id"])["status"] == "succeeded"
    assert client.post("/api/refresh", headers=headers).status_code == 202  # free again


def test_unknown_job_is_404(client):
    resp = client.get("/api/refresh/nope", headers=auth_headers(client, "admin"))
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "job_not_found")


def test_jobs_survive_a_restart_and_a_running_one_becomes_interrupted(api_settings, api_model, fake_extract):
    release = threading.Event()
    fake_extract(before=lambda: release.wait(10))
    with TestClient(create_app(api_settings)) as first:
        job = first.post("/api/refresh", headers=auth_headers(first, "admin")).json()
        # Simulate the server dying now: a new app reads the job files while this job is "running".
        with TestClient(create_app(api_settings)) as second:
            seen = second.get(f"/api/refresh/{job['job_id']}", headers=auth_headers(second, "admin")).json()
        release.set()
    assert seen["status"] == "interrupted"
    assert seen["error"] == "the server stopped during this refresh"
