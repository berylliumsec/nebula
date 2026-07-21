"""Add the durable one-shot zero-setup bootstrap marker.

Revision ID: 0004_zero_setup_bootstrap
Revises: 0003_operation_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0004_zero_setup_bootstrap"
down_revision = "0003_operation_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh local databases persist their one-shot eligibility decision before
    # the migration chain runs. Recovery after an interrupted migration must
    # reuse that table instead of attempting to create it again. Existing and
    # offline migration flows continue to let Alembic emit the authoritative
    # table DDL.
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table(
        "bootstrap_state"
    ):
        return
    op.create_table(
        "bootstrap_state",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("engagement_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("bootstrap_state")
