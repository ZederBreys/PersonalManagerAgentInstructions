"""add events.reminder_offsets

Revision ID: 0003_event_reminder_offsets
Revises: 0002_event_anchor_and_reminder_unique
Create Date: 2026-09-27

Persists the per-event reminder lead-time configuration so it survives
reminder regeneration and annual advancement. Stored as a JSON list of
non-negative integers (days before the event); the default is ``[0]``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_event_reminder_offsets"
down_revision = "0002_event_anchor_and_reminder_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column(
            "reminder_offsets",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[0]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("events", "reminder_offsets")
