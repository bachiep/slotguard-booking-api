"""Create resources and bookings tables with composite overlap index.

Revision ID: 0003_resources_and_bookings
Revises: 0002_auth_tables
Create Date: 2026-11-18 02:50:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_resources_and_bookings"
down_revision: str | Sequence[str] | None = "0002_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("hourly_rate_cents", sa.Integer(), nullable=False),
        sa.Column("open_time", sa.Time(), nullable=False),
        sa.Column("close_time", sa.Time(), nullable=False),
        sa.Column("slot_duration_minutes", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_resources_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resources")),
        sa.UniqueConstraint("name", name=op.f("uq_resources_name")),
    )
    op.create_index(op.f("ix_resources_owner_id"), "resources", ["owner_id"], unique=False)
    op.create_index(
        op.f("ix_resources_resource_type"),
        "resources",
        ["resource_type"],
        unique=False,
    )

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_price_cents", sa.Integer(), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_reference", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            name=op.f("fk_bookings_resource_id_resources"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_bookings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
    )
    op.create_index(
        "ix_bookings_resource_status_window",
        "bookings",
        ["resource_id", "status", "start_time", "end_time"],
        unique=False,
    )
    op.create_index(
        "ix_bookings_status_hold_expires_at",
        "bookings",
        ["status", "hold_expires_at"],
        unique=False,
    )
    op.create_index(op.f("ix_bookings_user_id"), "bookings", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_bookings_user_id"), table_name="bookings")
    op.drop_index("ix_bookings_status_hold_expires_at", table_name="bookings")
    op.drop_index("ix_bookings_resource_status_window", table_name="bookings")
    op.drop_table("bookings")
    op.drop_index(op.f("ix_resources_resource_type"), table_name="resources")
    op.drop_index(op.f("ix_resources_owner_id"), table_name="resources")
    op.drop_table("resources")
