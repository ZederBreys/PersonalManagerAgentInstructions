"""add notification_sent_at to job_runs

Revision ID: 0005_job_run_notification_sent_at
Revises: 0004_job_runs
Create Date: 2026-09-27

Persistent notification-delivery marker for a job run. ``NULL`` means a
notification has not been sent yet; a non-``NULL`` timestamp means it has,
which makes notifications idempotent across restarts.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_job_run_notification_sent_at"
down_revision = "0004_job_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_runs",
        sa.Column("notification_sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("job_runs", schema=None) as batch_op:
        batch_op.drop_column("notification_sent_at")
