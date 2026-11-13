"""Tie execution admission and idle retirement to project chat lifecycle."""

from alembic import op
import sqlalchemy as sa


revision = "0048_chat_scoped_admission"
down_revision = "0047_runtime_work_batches"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project_ownership") as batch:
        batch.alter_column(
            "admission_requested",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
        batch.add_column(
            sa.Column(
                "retirement_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.add_column(
        "runtime_commands",
        sa.Column("chat_start", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text("UPDATE project_ownership SET admission_requested = false")
    )


def downgrade():
    raise RuntimeError(
        "Retain chat-scoped admission and retirement state; use compatible code rollback"
    )
