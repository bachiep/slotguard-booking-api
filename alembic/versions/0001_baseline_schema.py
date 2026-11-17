"""Initial Alembic baseline revision.

Revision ID: 0001_baseline_schema
Revises:
Create Date: 2026-11-18 02:44:00+00:00
"""

from collections.abc import Sequence

revision: str = "0001_baseline_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish baseline Alembic migration state."""


def downgrade() -> None:
    """Revert baseline Alembic migration state."""
