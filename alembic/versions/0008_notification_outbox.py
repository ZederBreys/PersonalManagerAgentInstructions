"""notification outbox, inbox classification retries, expense reminder lead time

Revision ID: 0008_notification_outbox
Revises: 0007_sheet_keys
Create Date: 2026-10-03

* ``notifications``: durable outbox for user notifications (at-least-once
  delivery, unique ``dedup_key`` so a logical notification is queued once).
* ``inbox_messages``: attempt bookkeeping so failed / abandoned classifications
  are retried with a backoff instead of being stuck forever.
* ``recurring_expenses.reminder_days_before``: payment reminder lead time
  (existing expenses get the default of 3 days).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_notification_outbox"
down_revision = "0007_sheet_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "sending", "delivered", name="notificationstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'delivered')", name="ck_notifications_status"
        ),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_status"), ["status"], unique=False)

    with op.batch_alter_table("inbox_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("recurring_expenses", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("reminder_days_before", sa.Integer(), nullable=False, server_default="3")
        )
        batch_op.create_check_constraint(
            "ck_recurring_expenses_reminder_days_nonneg", "reminder_days_before >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("recurring_expenses", schema=None) as batch_op:
        batch_op.drop_constraint("ck_recurring_expenses_reminder_days_nonneg", type_="check")
        batch_op.drop_column("reminder_days_before")

    with op.batch_alter_table("inbox_messages", schema=None) as batch_op:
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("last_attempt_at")
        batch_op.drop_column("attempt_count")

    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_status"))
    op.drop_table("notifications")
