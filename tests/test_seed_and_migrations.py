"""Deterministic seed idempotency and Alembic migration verification tests."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from scripts.seed_demo import seed_demo_data
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import MutableClock
from slotguard.models.booking import Booking
from slotguard.models.resource import Resource
from slotguard.models.user import User


async def test_seed_demo_is_deterministic_and_idempotent(
    db_session: AsyncSession,
    clock: MutableClock,
) -> None:
    first = await seed_demo_data(db_session, clock=clock)
    assert first.users_created == 3
    assert first.resources_created == 4
    assert first.bookings_created == 2

    second = await seed_demo_data(db_session, clock=clock)
    assert second.users_created == 0
    assert second.resources_created == 0
    assert second.bookings_created == 0

    user_count = int((await db_session.execute(select(func.count(User.id)))).scalar_one())
    resource_count = int((await db_session.execute(select(func.count(Resource.id)))).scalar_one())
    booking_count = int((await db_session.execute(select(func.count(Booking.id)))).scalar_one())
    assert (user_count, resource_count, booking_count) == (3, 4, 2)


def test_alembic_migrations_build_expected_tables_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "alembic_verify.db"
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")

    command.upgrade(alembic_cfg, "head")

    sync_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        inspector = inspect(sync_engine)
        tables = set(inspector.get_table_names())
        assert {
            "alembic_version",
            "users",
            "refresh_tokens",
            "resources",
            "bookings",
            "webhook_events",
            "outbox_notifications",
        }.issubset(tables)

        booking_indexes = {idx["name"] for idx in inspector.get_indexes("bookings")}
        assert "ix_bookings_resource_status_window" in booking_indexes
        assert "ix_bookings_status_hold_expires_at" in booking_indexes

        webhook_uniques = {
            tuple(uq["column_names"]) for uq in inspector.get_unique_constraints("webhook_events")
        }
        assert ("provider", "event_id") in webhook_uniques
    finally:
        sync_engine.dispose()
