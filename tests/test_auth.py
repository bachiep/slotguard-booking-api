"""Authentication, JWT expiry, refresh token rotation, reuse revocation, and RBAC tests."""

from typing import Annotated

from fastapi import APIRouter, Depends
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import require_roles
from slotguard.core.clock import MutableClock
from slotguard.core.security import hash_password, verify_password
from slotguard.models.user import RefreshToken, User, UserRole


def test_argon2_password_hash_and_verify() -> None:
    hashed = hash_password("SecretPassword!42")

    assert hashed.startswith("$argon2id$")
    assert verify_password("SecretPassword!42", hashed) is True
    assert verify_password("WrongPassword!42", hashed) is False


async def test_register_login_and_me_flow(client: AsyncClient) -> None:
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "Alice@Example.com",
            "password": "Password123!",
            "full_name": "Alice Nguyen",
        },
    )
    assert register_resp.status_code == 201
    reg_body = register_resp.json()
    assert reg_body["token_type"] == "bearer"
    assert reg_body["user"]["email"] == "alice@example.com"
    assert reg_body["user"]["role"] == "MEMBER"

    dup_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": "Password123!",
            "full_name": "Alice Duplicate",
        },
    )
    assert dup_resp.status_code == 409
    assert dup_resp.headers["content-type"].startswith("application/problem+json")

    bad_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "WrongPassword!"},
    )
    assert bad_login.status_code == 401

    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "Password123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    me_resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == "alice@example.com"


async def test_missing_or_expired_jwt_returns_401(
    client: AsyncClient,
    clock: MutableClock,
) -> None:
    missing_resp = await client.get("/api/v1/auth/me")
    assert missing_resp.status_code == 401

    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "clock@example.com",
            "password": "Password123!",
            "full_name": "Clock User",
        },
    )
    token = reg_resp.json()["access_token"]

    clock.advance(minutes=16)

    expired_resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert expired_resp.status_code == 401
    assert expired_resp.json()["detail"] == "Access token has expired."


async def test_refresh_rotation_and_reuse_detection_revokes_family(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "rotate@example.com",
            "password": "Password123!",
            "full_name": "Rotate User",
        },
    )
    token_1 = reg_resp.json()["refresh_token"]

    rotate_resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_1},
    )
    assert rotate_resp.status_code == 200
    token_2 = rotate_resp.json()["refresh_token"]
    assert token_2 != token_1

    reuse_resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_1},
    )
    assert reuse_resp.status_code == 401
    assert "reuse detected" in reuse_resp.json()["detail"].lower()

    token_2_after_breach = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": token_2},
    )
    assert token_2_after_breach.status_code == 401

    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert len(rows) == 2
    assert all(row.revoked_at is not None for row in rows)


async def test_logout_revokes_refresh_token_family(client: AsyncClient) -> None:
    reg_resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "logout@example.com",
            "password": "Password123!",
            "full_name": "Logout User",
        },
    )
    refresh_token = reg_resp.json()["refresh_token"]

    logout_resp = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_resp.status_code == 204

    refresh_after = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert refresh_after.status_code == 401


async def test_rbac_guard_enforces_owner_role(
    app,
    client: AsyncClient,
    user_factory,
    auth_headers_factory,
) -> None:
    probe_router = APIRouter()

    @probe_router.get("/api/v1/_owner-only")
    async def owner_only_probe(
        owner: Annotated[User, Depends(require_roles(UserRole.OWNER))],
    ) -> dict[str, str]:
        return {"owner": owner.email}

    app.include_router(probe_router)

    member = await user_factory("member@example.com", role=UserRole.MEMBER)
    owner = await user_factory("owner@example.com", role=UserRole.OWNER)

    member_resp = await client.get("/api/v1/_owner-only", headers=auth_headers_factory(member))
    assert member_resp.status_code == 403
    assert member_resp.headers["content-type"].startswith("application/problem+json")

    owner_resp = await client.get("/api/v1/_owner-only", headers=auth_headers_factory(owner))
    assert owner_resp.status_code == 200
    assert owner_resp.json() == {"owner": "owner@example.com"}
