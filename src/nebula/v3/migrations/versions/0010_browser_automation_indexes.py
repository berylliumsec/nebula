"""Add indexed projections for run-scoped browser automation entities.

Revision ID: 0010_browser_automation_indexes
Revises: 0009_harness_mobile_foundation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_browser_automation_indexes"
down_revision = "0009_harness_mobile_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("entities") as batch:
        batch.add_column(sa.Column("automation_run_id", sa.String(length=200)))
        batch.add_column(sa.Column("automation_session_id", sa.String(length=200)))
        batch.add_column(sa.Column("automation_status", sa.String(length=40)))
        batch.add_column(sa.Column("automation_expires_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_entities_automation_run_session",
        "entities",
        ["kind", "automation_run_id", "automation_session_id"],
    )
    op.create_index(
        "ix_entities_automation_status_expiry",
        "entities",
        ["kind", "automation_status", "automation_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_entities_automation_status_expiry", table_name="entities")
    op.drop_index("ix_entities_automation_run_session", table_name="entities")
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("automation_expires_at")
        batch.drop_column("automation_status")
        batch.drop_column("automation_session_id")
        batch.drop_column("automation_run_id")
