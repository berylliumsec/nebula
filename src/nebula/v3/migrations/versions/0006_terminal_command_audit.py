"""Promote command-only history to durable terminal audit records.

Revision ID: 0006_terminal_command_audit
Revises: 0005_terminal_command_history
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0006_terminal_command_audit"
down_revision = "0005_terminal_command_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("terminal_command_records") as batch:
        batch.add_column(
            sa.Column(
                "operator_id",
                sa.String(length=200),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("shell_sequence", sa.String(length=200)))
        batch.add_column(sa.Column("command_sha256", sa.String(length=64)))
        batch.add_column(
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="legacy_metadata_only",
            )
        )
        batch.alter_column("exit_code", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("raw_output_artifact_id", sa.String(length=200)))
        batch.add_column(
            sa.Column("redacted_output_artifact_id", sa.String(length=200))
        )
        batch.add_column(
            sa.Column(
                "observed_output_bytes",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "captured_output_bytes",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("output_sha256", sa.String(length=64)))
        batch.add_column(
            sa.Column(
                "output_truncated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("output_preview", sa.Text(), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("capture_error", sa.Text()))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, command FROM terminal_command_records")
    ).mappings()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE terminal_command_records SET "
                "command_sha256 = :command_sha256, "
                "status = 'legacy_metadata_only', "
                "started_at = occurred_at, completed_at = occurred_at, "
                "capture_error = :capture_error WHERE id = :record_id"
            ),
            {
                "record_id": row["id"],
                "command_sha256": hashlib.sha256(
                    str(row["command"]).encode("utf-8")
                ).hexdigest(),
                "capture_error": (
                    "result output and operator attribution were not captured "
                    "by the legacy terminal history"
                ),
            },
        )
    op.create_index(
        "ix_terminal_commands_project_operator",
        "terminal_command_records",
        ["engagement_id", "operator_id"],
    )
    op.create_index(
        "ix_terminal_commands_project_status",
        "terminal_command_records",
        ["engagement_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_terminal_commands_project_status",
        table_name="terminal_command_records",
    )
    op.drop_index(
        "ix_terminal_commands_project_operator",
        table_name="terminal_command_records",
    )
    op.execute(
        sa.text(
            "UPDATE terminal_command_records SET exit_code = -1 "
            "WHERE exit_code IS NULL"
        )
    )
    with op.batch_alter_table("terminal_command_records") as batch:
        batch.drop_column("capture_error")
        batch.drop_column("output_preview")
        batch.drop_column("output_truncated")
        batch.drop_column("output_sha256")
        batch.drop_column("captured_output_bytes")
        batch.drop_column("observed_output_bytes")
        batch.drop_column("redacted_output_artifact_id")
        batch.drop_column("raw_output_artifact_id")
        batch.drop_column("completed_at")
        batch.drop_column("started_at")
        batch.alter_column("exit_code", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("status")
        batch.drop_column("command_sha256")
        batch.drop_column("shell_sequence")
        batch.drop_column("operator_id")
