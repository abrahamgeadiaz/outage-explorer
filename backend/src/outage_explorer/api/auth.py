"""Authentication: password check, signed access tokens, and the `current_user` / `require` dependencies.

Simplified on purpose (the brief allows it): users are seeded in users.py, passwords are hashed with
scrypt (stdlib), and the API issues short-lived HS256 JWTs. The token carries only who the user is
(`sub`) and its own id (`jti`); the role is looked up on every request, so a role change applies at
once instead of when the token expires. Signing out revokes the token's id until it would expire.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from outage_explorer.api.errors import ApiError, forbidden, unauthenticated
from outage_explorer.api.policy import ROLE_CAPABILITIES, Capability, can
from outage_explorer.api.ratelimit import LoginLimiter
from outage_explorer.api.users import USERS, User
from outage_explorer.config import Settings

ALGORITHM = "HS256"
ISSUER = "outage-explorer"
MIN_SECRET_LENGTH = 32

# scrypt cost: ~50 ms per check, fine for a login endpoint; n=2**14, r=8, p=1 are the RFC 7914 defaults.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    scheme, salt, digest = stored.split("$")
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), dklen=32, **_SCRYPT)
    return hmac.compare_digest(candidate, _unb64(digest))


# Checked when the username is unknown, so a wrong username takes as long as a wrong password
# (the response time does not reveal which usernames exist).
_DUMMY_HASH = hash_password("not-a-real-password", salt=b"\0" * 16)


def authenticate(username: str, password: str) -> User | None:
    user = USERS.get(username)
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    return user if user and ok else None


def check_secret(settings: Settings) -> None:
    if len(settings.jwt_secret.get_secret_value()) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters; see .env.example"
        )


def issue_token(settings: Settings, user: User, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    claims = {
        "sub": user.username,
        "jti": secrets.token_urlsafe(16),
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


class RevokedTokens:
    """Ids of signed-out tokens, each kept until its token would have expired anyway.

    In memory, per process: a restart forgets them (tokens live 60 minutes at most). Expired ids are
    dropped on every revoke, so the set holds at most the tokens signed out in the last hour.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    def revoke(self, jti: str, expires_at: float) -> None:
        now = self.clock()
        with self._lock:
            self._expiry = {j: exp for j, exp in self._expiry.items() if exp > now}
            self._expiry[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            return jti in self._expiry


def decode_claims(settings: Settings, token: str) -> dict:
    """A token's claims; raises 401 for anything not signed by us, expired, or malformed."""
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],  # pinned: never let the token choose (no "none", no key confusion)
            issuer=ISSUER,
            options={"require": ["sub", "jti", "iss", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise unauthenticated("access token expired") from None
    except jwt.InvalidTokenError:
        raise unauthenticated() from None


_bearer = HTTPBearer(auto_error=False)  # auto_error would answer 403; a missing token is a 401


def current_claims(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> dict:
    if credentials is None:
        raise unauthenticated()
    claims = decode_claims(request.app.state.settings, credentials.credentials)
    if request.app.state.revoked_tokens.is_revoked(claims["jti"]):
        raise unauthenticated("signed out; sign in again")
    return claims


def current_user(claims: Annotated[dict, Depends(current_claims)]) -> User:
    user = USERS.get(claims["sub"])
    if user is None:  # deleted since the token was issued
        raise unauthenticated()
    return user


def require(capability: Capability):
    """Dependency: the current user, if their role has `capability`; 403 otherwise."""

    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if not can(user.role, capability):
            raise forbidden(f"role '{user.role}' cannot use '{capability}'")
        return user

    return dependency


# --- endpoints ----------------------------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


# Checked before the handler runs, so an oversized login is a 422 that costs neither a scrypt hash nor
# a rate-limiter entry. Generous for real passwords (NIST asks for at least 64 characters).
MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_LENGTH = 256


class LoginRequest(BaseModel):
    username: str = Field(max_length=MAX_USERNAME_LENGTH)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class UserOut(BaseModel):
    username: str
    display_name: str
    role: str
    capabilities: list[str]

    @classmethod
    def of(cls, user: User) -> "UserOut":
        caps = sorted(ROLE_CAPABILITIES[user.role])
        return cls(username=user.username, display_name=user.display_name, role=user.role, capabilities=caps)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 (the OAuth2 scheme name, not a secret)
    expires_in: int  # seconds
    user: UserOut


@router.post("/login", response_model=TokenOut, responses={429: {"description": "Too many failed logins"}})
def login(body: LoginRequest, request: Request) -> TokenOut:
    settings: Settings = request.app.state.settings
    limiter: LoginLimiter = request.app.state.login_limiter
    client = request.client.host if request.client else "unknown"
    if (wait := limiter.retry_after(client, body.username)) is not None:
        # Before the password check: while blocked, guessing (even right) gains nothing.
        raise ApiError(
            429,
            "rate_limited",
            f"too many failed logins; try again in {wait} s",
            headers={"Retry-After": str(wait)},
        )
    user = authenticate(body.username, body.password)
    if user is None:
        limiter.failed(client, body.username)
        # Same answer for an unknown user and a wrong password.
        raise ApiError(401, "invalid_credentials", "invalid username or password")
    limiter.succeeded(client, body.username)
    return TokenOut(
        access_token=issue_token(settings, user),
        expires_in=settings.jwt_ttl_minutes * 60,
        user=UserOut.of(user),
    )


@router.post("/logout", status_code=204)
def logout(claims: Annotated[dict, Depends(current_claims)], request: Request) -> None:
    """Revoke this token: every later request with it is a 401, as if it had expired."""
    request.app.state.revoked_tokens.revoke(claims["jti"], claims["exp"])


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(current_user)]) -> UserOut:
    return UserOut.of(user)
