"""Authentication: login, token validation, capability checks, and the error shape."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from outage_explorer.api import auth
from outage_explorer.api.app import create_app
from outage_explorer.api.policy import Capability, Role, can
from outage_explorer.api.users import USERS, User
from outage_explorer.config import Settings
from tests.conftest import PASSWORDS, TEST_JWT_SECRET, auth_headers


def _token(claims: dict, secret: str = TEST_JWT_SECRET, algorithm: str = "HS256") -> dict[str, str]:
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm=algorithm)}"}


def _claims(**overrides) -> dict:
    now = datetime.now(UTC)
    return {"sub": "analyst", "iss": auth.ISSUER, "iat": now, "exp": now + timedelta(minutes=5), **overrides}


@pytest.mark.parametrize("username", ["analyst", "viewer", "admin"])
def test_every_seeded_user_logs_in_with_the_documented_password(client, username):
    resp = client.post("/api/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["user"]["role"] == username  # one user per persona, named after it
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["username"] == username


def test_only_admin_has_refresh():
    assert {r for r in Role if can(r, Capability.REFRESH)} == {Role.ADMIN}
    assert all(can(r, Capability.CATALOG) for r in Role)


def test_capabilities_are_reported_to_the_client(client):
    me = client.get("/api/auth/me", headers=auth_headers(client, "viewer")).json()
    assert me["capabilities"] == ["catalog", "preview", "query"]


@pytest.mark.parametrize(
    "username,password", [("analyst", "wrong"), ("nobody", "analyst-demo-pass"), ("analyst", "")]
)
def test_bad_credentials_get_one_answer_that_does_not_say_which_part_was_wrong(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {"code": "invalid_credentials", "message": "invalid username or password"}
    }


def test_missing_token_is_401_with_bearer_challenge(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    assert resp.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic YW5hbHlzdDphbmFseXN0LWRlbW8tcGFzcw=="},  # right password, wrong scheme
        _token(_claims(), secret="some-other-secret-" + "y" * 32),  # forged: not signed by us
        _token(_claims(), algorithm="none", secret=None),  # unsigned
        _token(_claims(sub="mallory")),  # well signed, but no such user
        _token(_claims(iss="someone-else")),
        _token({k: v for k, v in _claims().items() if k != "exp"}),  # never expires: refused
    ],
    ids=["garbage", "basic", "wrong-secret", "alg-none", "unknown-user", "wrong-issuer", "no-exp"],
)
def test_invalid_tokens_are_rejected(client, headers):
    resp = client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"


def test_expired_token_is_rejected_and_says_so(client):
    past = datetime.now(UTC) - timedelta(hours=2)
    resp = client.get("/api/auth/me", headers=_token(_claims(iat=past, exp=past + timedelta(minutes=60))))
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "access token expired"


def test_role_comes_from_the_user_store_not_the_token(client, monkeypatch):
    headers = auth_headers(client, "analyst")
    demoted = User("analyst", "Ana Analyst", Role.VIEWER, USERS["analyst"].password_hash)
    monkeypatch.setitem(USERS, "analyst", demoted)
    assert client.get("/api/auth/me", headers=headers).json()["role"] == "viewer"


def test_require_rejects_a_role_without_the_capability(api_settings):
    app = create_app(api_settings)

    @app.post("/test-refresh")
    def _refresh(user: Annotated[User, Depends(auth.require(Capability.REFRESH))]) -> dict:
        return {"ok": user.username}

    with TestClient(app) as c:
        for name, status in [("viewer", 403), ("analyst", 403), ("admin", 200)]:
            resp = c.post("/test-refresh", headers=auth_headers(c, name))
            assert resp.status_code == status, name
        assert c.post("/test-refresh").status_code == 401  # authentication is checked first


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_app_refuses_to_start_without_a_strong_secret(tmp_path, secret):
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app(Settings(data_dir=tmp_path, jwt_secret=secret))


def test_password_hashes_are_salted_and_verify():
    a, b = auth.hash_password("pw"), auth.hash_password("pw")
    assert a != b
    assert auth.verify_password("pw", a) and auth.verify_password("pw", b)
    assert not auth.verify_password("pW", a)


@pytest.mark.parametrize(
    "field,length", [("username", auth.MAX_USERNAME_LENGTH + 1), ("password", auth.MAX_PASSWORD_LENGTH + 1)]
)
def test_oversized_login_fields_are_refused_before_hashing_or_counting(client, monkeypatch, field, length):
    hashed, counted = [], []
    monkeypatch.setattr(auth, "verify_password", lambda *a: hashed.append(a) or False)
    monkeypatch.setattr(client.app.state.login_limiter, "failed", lambda *a: counted.append(a))
    body = {"username": "analyst", "password": "x", field: "x" * length}
    resp = client.post("/api/auth/login", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["field"] == f"body.{field}"
    assert hashed == [] and counted == []


def test_the_longest_allowed_fields_still_get_a_normal_answer(client):
    body = {"username": "x" * auth.MAX_USERNAME_LENGTH, "password": "x" * auth.MAX_PASSWORD_LENGTH}
    assert client.post("/api/auth/login", json=body).json()["error"]["code"] == "invalid_credentials"


def test_validation_errors_use_the_common_error_shape(client):
    resp = client.post("/api/auth/login", json={"username": "analyst"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"][0]["field"] == "body.password"


def test_health_needs_no_login_and_reports_no_model_yet(client):
    assert client.get("/api/health").json() == {"status": "ok", "model_build": None}
