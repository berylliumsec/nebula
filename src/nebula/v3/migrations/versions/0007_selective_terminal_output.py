"""Add selective terminal output recording policy and provenance.

Revision ID: 0007_selective_terminal_output
Revises: 0006_terminal_command_audit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_selective_terminal_output"
down_revision = "0006_terminal_command_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("terminal_command_preferences") as batch:
        batch.add_column(
            sa.Column(
                "custom_tools", sa.Text(), nullable=False, server_default="[]"
            )
        )
        batch.add_column(
            sa.Column(
                "disabled_tools", sa.Text(), nullable=False, server_default="[]"
            )
        )
        batch.add_column(
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0")
        )
    op.execute(sa.text("UPDATE terminal_command_preferences SET enabled = true"))

    with op.batch_alter_table("terminal_command_records") as batch:
        batch.add_column(
            sa.Column(
                "capture_decision",
                sa.String(length=40),
                nullable=False,
                server_default="legacy_metadata_only",
            )
        )
        batch.add_column(
            sa.Column(
                "matched_tools", sa.Text(), nullable=False, server_default="[]"
            )
        )
        batch.add_column(sa.Column("recording_policy_revision", sa.Integer()))
        batch.add_column(sa.Column("runtime_image_digest", sa.String(length=71)))
    op.execute(
        sa.text(
            "UPDATE terminal_command_records SET capture_decision = "
            "CASE WHEN status = 'legacy_metadata_only' "
            "THEN 'legacy_metadata_only' ELSE 'legacy_all_commands' END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("terminal_command_records") as batch:
        batch.drop_column("runtime_image_digest")
        batch.drop_column("recording_policy_revision")
        batch.drop_column("matched_tools")
        batch.drop_column("capture_decision")
    with op.batch_alter_table("terminal_command_preferences") as batch:
        batch.drop_column("revision")
        batch.drop_column("disabled_tools")
        batch.drop_column("custom_tools")
