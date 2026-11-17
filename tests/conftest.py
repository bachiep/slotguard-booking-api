"""Shared async pytest fixtures with isolated SQLite WAL databases and deterministic clock."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from slotguard.core.clock import MutableClock
from slotguard.core.config import Settings
from slotguard.core.database import Base, create_engine_and_sessionmaker
from slotguard.core.security import create_access_token, hash_password
from slotguard.main import create_app
from slotguard.models.user import User, UserRole


@pytest.fixture
def clock() -> MutableClock:
    """Provide a deterministic UTC clock starting at a fixed instant."""
    return MutableClock(datetime(2026, 12, 1, 8, 0, 0, tzinfo=UTC))


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Build isolated test settings backed by a temporary SQLite WAL database."""
    db_path = tmp_path / "slotguard_test.db"
    return Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
        rate_limit_backend="memory",
        worker_enabled=False,
        jwt_secret="test-secret-key-at-least-32-characters-long",
        webhook_secret="test-webhook-secret-key-32-characters",
    )


@pytest.fixture
async def db_engine_and_session_maker(
    test_settings: Settings,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Create schema on a fresh temporary database and dispose cleanly after test."""
    engine, session_maker = create_engine_and_sessionmaker(test_settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine, session_maker
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[AsyncSession]:
    """Provide a direct AsyncSession for repository/service tests."""
    _, session_maker = db_engine_and_session_maker
    async with session_maker() as session:
        yield session


@pytest.fixture
def app(
    test_settings: Settings,
    clock: MutableClock,
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
):
    """Build a FastAPI application wired to isolated test DB and MutableClock."""
    engine, session_maker = db_engine_and_session_maker
    return create_app(
        settings=test_settings,
        clock=clock,
        engine=engine,
        session_maker=session_maker,
    )


@pytest.fixture
async def client(app) -> AsyncIterator[AsyncClient]:
    """Provide an async HTTPX client targeting the FastAPI ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


@pytest.fixture
def user_factory(
    db_engine_and_session_maker: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    clock: MutableClock,
) -> Callable[..., Awaitable[User]]:
    """Create and persist a user with configurable role."""
    _, session_maker = db_engine_and_session_maker

    async def _create_user(
        email: str,
        *,
        password: str = "StrongPass123!",
        full_name: str = "Test User",
        role: UserRole = UserRole.MEMBER,
        is_active: bool = True,
    ) -> User:
        async with session_maker() as session:
            user = User(
                email=email.lower().strip(),
                password_hash=hash_password(password),
                full_name=full_name,
                role=role.value,
                is_active=is_active,
                created_at=clock.now(),
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    return _create_user


@pytest.fixture
def auth_headers_factory(
    test_settings: Settings,
    clock: MutableClock,
) -> Callable[[User], dict[str, str]]:
    """Generate Authorization headers for a persisted User using the test clock."""

    def _headers(user: User) -> dict[str, str]:
        token = create_access_token(
            user_id=user.id,
            email=user.email,
            role=user.role,
            settings=test_settings,
            clock=clock,
        )
        return {"Authorization": f"Bearer {token}"}

    return _headers
