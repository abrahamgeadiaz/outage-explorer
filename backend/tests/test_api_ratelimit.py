"""Login rate limiting: per (client, username) and per client, sliding window, Retry-After."""

import pytest
from fastapi.testclient import TestClient

from outage_explorer.api import auth
from outage_explorer.api.app import create_app
from outage_explorer.api.ratelimit import MAX_TRACKED_KEYS, LoginLimiter
from tests.conftest import PASSWORDS


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _fail(client, username, times):
    for _ in range(times):
        assert _login(client, username, "wrong").status_code == 401


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def app(api_settings, clock):
    app = create_app(api_settings)
    app.state.login_limiter = LoginLimiter(5, 20, window_seconds=300, clock=lambda: clock[0])
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_five_failures_block_the_username_even_with_the_right_password(client):
    _fail(client, "analyst", 5)
    resp = _login(client, "analyst", PASSWORDS["analyst"])
    assert resp.status_code == 429
    assert resp.json()["error"] == {
        "code": "rate_limited",
        "message": "too many failed logins; try again in 300 s",
    }
    assert resp.headers["Retry-After"] == "300"


def test_blocked_attempts_do_not_check_the_password(client, monkeypatch):
    _fail(client, "analyst", 5)
    checked = []
    monkeypatch.setattr(auth, "authenticate", lambda u, p: checked.append(u))
    _login(client, "analyst", "guess")
    assert checked == []


def test_the_block_lifts_when_failures_leave_the_window(client, clock):
    _fail(client, "analyst", 1)
    clock[0] += 100
    _fail(client, "analyst", 4)
    assert _login(client, "analyst", "x").headers["Retry-After"] == "200"  # oldest failure leaves at +300
    clock[0] += 200
    assert _login(client, "analyst", PASSWORDS["analyst"]).status_code == 200


def test_username_case_and_spaces_count_as_one(client):
    for name in ("analyst", "Analyst", " ANALYST", "analyst ", "aNaLyst"):
        _login(client, name, "wrong")
    assert _login(client, "analyst", PASSWORDS["analyst"]).status_code == 429


def test_a_success_clears_that_usernames_failures(client):
    _fail(client, "analyst", 4)
    assert _login(client, "analyst", PASSWORDS["analyst"]).status_code == 200
    _fail(client, "analyst", 4)
    assert _login(client, "analyst", PASSWORDS["analyst"]).status_code == 200


def test_other_users_from_the_same_client_are_not_blocked(client):
    _fail(client, "analyst", 5)
    assert _login(client, "viewer", PASSWORDS["viewer"]).status_code == 200


def test_spraying_many_usernames_blocks_the_client(client):
    for i in range(20):
        _login(client, f"user{i}", "password1")
    assert _login(client, "admin", PASSWORDS["admin"]).status_code == 429


def test_another_client_is_not_blocked(app):
    with TestClient(app, client=("10.0.0.1", 50000)) as attacker:
        _fail(attacker, "analyst", 5)
        assert _login(attacker, "analyst", PASSWORDS["analyst"]).status_code == 429
    with TestClient(app, client=("10.0.0.2", 50000)) as real_user:
        assert _login(real_user, "analyst", PASSWORDS["analyst"]).status_code == 200


def test_forwarded_for_header_is_not_trusted(app):
    with TestClient(app, client=("10.0.0.1", 50000)) as c:
        for i in range(5):
            c.post(
                "/api/auth/login",
                json={"username": "analyst", "password": "wrong"},
                headers={"X-Forwarded-For": f"203.0.113.{i}"},  # a new "IP" every time
            )
        assert _login(c, "analyst", PASSWORDS["analyst"]).status_code == 429


def test_defaults_come_from_settings(api_settings):
    app = create_app(api_settings.model_copy(update={"login_max_failures": 2}))
    with TestClient(app) as c:
        _fail(c, "analyst", 2)
        assert _login(c, "analyst", PASSWORDS["analyst"]).status_code == 429


def test_zero_disables_a_limit():
    limiter = LoginLimiter(0, 0, window_seconds=300)
    for _ in range(50):
        limiter.failed("c", "u")
    assert limiter.retry_after("c", "u") is None


def test_tracked_keys_are_capped():
    limiter = LoginLimiter(5, 0, window_seconds=300)
    for i in range(MAX_TRACKED_KEYS + 100):
        limiter.failed("c", f"user{i}")
    assert len(limiter._failures) <= MAX_TRACKED_KEYS


def test_retry_after_counts_from_the_failure_that_must_expire():
    # Concurrent requests can all pass the check before any failure is recorded, so a pair can hold
    # more failures than the limit. It is allowed again once it is back below the limit.
    now = [0.0]
    limiter = LoginLimiter(3, 0, window_seconds=100, clock=lambda: now[0])
    for t in (0, 10, 20, 30, 40):
        now[0] = t
        limiter.failed("c", "u")
    now[0] = 50
    # 5 failures, limit 3: allowed once 3 have expired, i.e. when t=20 leaves the window at 120.
    assert limiter.retry_after("c", "u") == 70
    now[0] = 119
    assert limiter.retry_after("c", "u") == 1
    now[0] = 120
    assert limiter.retry_after("c", "u") is None  # only t=30 and t=40 are left
