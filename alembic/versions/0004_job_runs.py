"""create job_runs

Revision ID: 0004_job_runs
Revises: 0003_event_reminder_offsets
Create Date: 2026-09-27

Persistent execution state for scheduled jobs. The database (not APScheduler)
is the source of truth for whether a job was scheduled, started, finished,
failed, or left stuck in ``running``.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_job_runs"
down_revision = "0003_event_reminder_offsets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_name", sa.String(length=200), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "success",
                "failed",
                name="jobrunstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed')",
            name="ck_job_runs_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("job_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_runs_job_name"), ["job_name"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_job_runs_scheduled_at"), ["scheduled_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_job_runs_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("job_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_runs_status"))
        batch_op.drop_index(batch_op.f("ix_job_runs_scheduled_at"))
        batch_op.drop_index(batch_op.f("ix_job_runs_job_name"))

    op.drop_table("job_runs")
