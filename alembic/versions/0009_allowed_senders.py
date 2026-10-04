"""allowed senders (the Email sheet)

Revision ID: 0009_allowed_senders
Revises: 0008_notification_outbox
Create Date: 2026-10-04

The Gmail import reads only messages from these addresses. They used to be a
constant in the code; the two addresses that were there are inserted here so
nothing changes for an existing installation.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone


revision = "0009_allowed_senders"
down_revision = "0008_notification_outbox"
branch_labels = None
depends_on = None

_SEED = ("support@liteserver.nl", "admin@ztv.su")


def upgrade() -> None:
    table = op.create_table(
        "allowed_senders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sheet_key", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("email <> ''", name="ck_allowed_senders_email_nonempty"),
        sa.CheckConstraint("email = lower(email)", name="ck_allowed_senders_email_lowercase"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    with op.batch_alter_table("allowed_senders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_allowed_senders_sheet_key"), ["sheet_key"], unique=True)
    op.bulk_insert(
        table,
        [{"email": email, "is_active": True, "created_at": datetime.now(timezone.utc).replace(tzinfo=None)} for email in _SEED],
    )


def downgrade() -> None:
    with op.batch_alter_table("allowed_senders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_allowed_senders_sheet_key"))
    op.drop_table("allowed_senders")
