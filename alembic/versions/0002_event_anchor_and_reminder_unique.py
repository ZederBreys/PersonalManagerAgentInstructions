"""add event anchor_date + unique (event_id, remind_at) on reminders

Revision ID: 0002_event_anchor_and_reminder_unique
Revises: d0a81bd19d00
Create Date: 2026-09-27

This migration supports the Events/Reminders domain logic:

* ``events.anchor_date`` (nullable) stores the canonical annual anchor for a
  YEARLY event so Feb 29 does not drift to Feb 28 across non-leap years.
* ``uq_reminders_event_id_remind_at`` makes reminder generation idempotent
  (one reminder per event per calendar date).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_event_anchor_and_reminder_unique"
down_revision = "d0a81bd19d00"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("anchor_date", sa.Date(), nullable=True))

    with op.batch_alter_table("reminders") as batch_op:
        batch_op.create_unique_constraint(
            "uq_reminders_event_id_remind_at", ["event_id", "remind_at"]
        )


def downgrade() -> None:
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.drop_constraint("uq_reminders_event_id_remind_at", type_="unique")

    op.drop_column("events", "anchor_date")
