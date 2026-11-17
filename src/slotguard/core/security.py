"""Password hashing (Argon2id via pwdlib), JWT issuance/verification, and token hashing."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.exceptions import UnauthorizedError

_password_hasher = PasswordHash((Argon2Hasher(),))


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Verified claims extracted from a JWT access token."""

    user_id: int
    email: str
    role: str
    jti: str


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using Argon2id."""
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2id hash."""
    return _password_hasher.verify(plain_password, password_hash)


def generate_refresh_token() -> str:
    """Generate a high-entropy opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(raw_token: str) -> str:
    """Compute a deterministic SHA-256 hex digest for token storage."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    user_id: int,
    email: str,
    role: str,
    settings: Settings,
    clock: Clock,
) -> str:
    """Issue a signed JWT access token bound to the injected clock."""
    now = clock.now()
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(
    token: str,
    *,
    settings: Settings,
    clock: Clock,
) -> AccessTokenClaims:
    """Validate JWT signature, token type, and expiration against the injected clock."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["sub", "exp", "iat", "type", "role", "email"],
            },
        )
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid access token.") from exc

    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid token type.")

    try:
        exp_ts = int(payload["exp"])
        iat_ts = int(payload["iat"])
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise UnauthorizedError("Malformed access token claims.") from exc

    now_ts = int(clock.now().timestamp())
    if iat_ts > now_ts:
        raise UnauthorizedError("Access token is not valid yet.")
    if exp_ts <= now_ts:
        raise UnauthorizedError("Access token has expired.")

    return AccessTokenClaims(
        user_id=user_id,
        email=str(payload["email"]),
        role=str(payload["role"]),
        jti=str(payload.get("jti", "")),
    )
