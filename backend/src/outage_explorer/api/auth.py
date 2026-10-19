"""Authentication: password check, signed access tokens, and the `current_user` / `require` dependencies.

Simplified on purpose (the brief allows it): users are seeded in users.py, passwords are hashed with
scrypt (stdlib), and the API issues short-lived HS256 JWTs. The token carries only who the user is
(`sub`); the role is looked up on every request, so a role change applies at once instead of when
the token expires.
"""

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from outage_explorer.api.errors import ApiError, forbidden, unauthenticated
from outage_explorer.api.policy import ROLE_CAPABILITIES, Capability, can
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
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_ttl_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_token(settings: Settings, token: str) -> User:
    """The user a token belongs to; raises 401 for anything not signed by us, expired, or malformed."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],  # pinned: never let the token choose (no "none", no key confusion)
            issuer=ISSUER,
            options={"require": ["sub", "iss", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise unauthenticated("access token expired") from None
    except jwt.InvalidTokenError:
        raise unauthenticated() from None
    user = USERS.get(claims["sub"])
    if user is None:  # deleted since the token was issued
        raise unauthenticated()
    return user


_bearer = HTTPBearer(auto_error=False)  # auto_error would answer 403; a missing token is a 401


def current_user(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> User:
    if credentials is None:
        raise unauthenticated()
    return decode_token(request.app.state.settings, credentials.credentials)


def require(capability: Capability):
    """Dependency: the current user, if their role has `capability`; 403 otherwise."""

    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if not can(user.role, capability):
            raise forbidden(f"role '{user.role}' cannot use '{capability}'")
        return user

    return dependency


# --- endpoints ----------------------------------------------------------------------------------

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


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


@router.post("/login", response_model=TokenOut)
def login(body: LoginRequest, request: Request) -> TokenOut:
    settings: Settings = request.app.state.settings
    user = authenticate(body.username, body.password)
    if user is None:
        # Same answer for an unknown user and a wrong password.
        raise ApiError(401, "invalid_credentials", "invalid username or password")
    return TokenOut(
        access_token=issue_token(settings, user),
        expires_in=settings.jwt_ttl_minutes * 60,
        user=UserOut.of(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(current_user)]) -> UserOut:
    return UserOut.of(user)
