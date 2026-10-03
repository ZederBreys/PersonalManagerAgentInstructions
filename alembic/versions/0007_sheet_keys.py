"""add sheet_key to events and recurring_expenses

Revision ID: 0007_sheet_keys
Revises: 0006_inbox_messages
Create Date: 2026-10-03

A nullable, unique one-time key of the Google Sheets row a record was created
from. It makes creating records from new sheet rows idempotent: a retried sync
finds the already-created record by this key instead of creating a duplicate.
Existing rows get NULL (SQLite allows many NULLs in a unique index).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_sheet_keys"
down_revision = "0006_inbox_messages"
branch_labels = None
depends_on = None

_TABLES = ("events", "recurring_expenses")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("sheet_key", sa.String(length=32), nullable=True))
            batch_op.create_index(
                batch_op.f(f"ix_{table}_sheet_key"), ["sheet_key"], unique=True
            )


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f"ix_{table}_sheet_key"))
            batch_op.drop_column("sheet_key")
