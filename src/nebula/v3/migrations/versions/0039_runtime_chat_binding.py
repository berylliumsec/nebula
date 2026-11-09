"""Bind accepted chat commands to their durable turns without a crash gap."""

from alembic import op
import sqlalchemy as sa

revision = "0039_runtime_chat_binding"
down_revision = "0038_runtime_index_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_chat_bindings",
        sa.Column("command_id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("turn_id", sa.String(200), nullable=False),
        sa.Column("session_id", sa.String(200), nullable=False),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_runtime_chat_binding_turn",
        "runtime_chat_bindings",
        ["project_id", "turn_id"],
    )
    op.create_index("ix_runtime_command_name", "runtime_commands", ["name"])


def downgrade():
    raise RuntimeError(
        "Retain accepted chat identities; roll back compatible code without restoring SQL"
    )
