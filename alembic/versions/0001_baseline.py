"""baseline (empty schema)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-27

The baseline is intentionally empty: no domain tables exist yet. It only
establishes the Alembic revision chain so future model migrations can build
on top of it. Applying it creates the ``alembic_version`` bookkeeping table.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
