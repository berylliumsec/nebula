"""Add local, command-only terminal history.

Revision ID: 0005_terminal_command_history
Revises: 0004_zero_setup_bootstrap
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_terminal_command_history"
down_revision = "0004_zero_setup_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "terminal_command_preferences",
        sa.Column("engagement_id", sa.String(length=200), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["entities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("engagement_id"),
    )
    op.create_table(
        "terminal_command_records",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("engagement_id", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=200), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["engagement_id"],
            ["entities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_commands_project_time",
        "terminal_command_records",
        ["engagement_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_terminal_commands_project_session",
        "terminal_command_records",
        ["engagement_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_terminal_commands_project_session",
        table_name="terminal_command_records",
    )
    op.drop_index(
        "ix_terminal_commands_project_time",
        table_name="terminal_command_records",
    )
    op.drop_table("terminal_command_records")
    op.drop_table("terminal_command_preferences")
