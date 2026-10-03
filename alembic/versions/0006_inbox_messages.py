"""create inbox_messages

Revision ID: 0006_inbox_messages
Revises: 0005_job_run_notification_sent_at
Create Date: 2026-09-28

Internal intake of incoming messages (e.g. from Gmail). The ``(source,
external_id)`` unique constraint makes re-importing the same external message
idempotent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_inbox_messages"
down_revision = "0005_job_run_notification_sent_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("sender", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "processing",
                "processed",
                "failed",
                name="inboxstatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("classification", sa.String(length=50), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source", "external_id", name="uq_inbox_messages_source_external_id"
        ),
        sa.CheckConstraint(
            "status IN ('new', 'processing', 'processed', 'failed')",
            name="ck_inbox_messages_status",
        ),
    )
    with op.batch_alter_table("inbox_messages", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_inbox_messages_status"), ["status"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("inbox_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inbox_messages_status"))

    op.drop_table("inbox_messages")
