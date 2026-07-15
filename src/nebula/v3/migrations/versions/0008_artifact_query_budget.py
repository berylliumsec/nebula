"""Add the independent artifact retrieval budget.

Revision ID: 0008_artifact_query_budget
Revises: 0007_selective_terminal_output
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_artifact_query_budget"
down_revision = "0007_selective_terminal_output"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run_budget_counters") as batch:
        batch.add_column(
            sa.Column(
                "artifact_queries", sa.Integer(), nullable=False, server_default="0"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("run_budget_counters") as batch:
        batch.drop_column("artifact_queries")
