"""Keep SQL-only Work batch participants independent of API worker lifetime."""

from alembic import op
import sqlalchemy as sa

revision = "0047_runtime_work_batches"
down_revision = "0046_provider_tool_manifest"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_work_batch_projects",
        sa.Column(
            "batch_id",
            sa.String(200),
            sa.ForeignKey("runtime_commands.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("barrier_command_id", sa.String(200), nullable=True),
        sa.Column("barrier_sequence", sa.BigInteger(), nullable=True),
        sa.Column("runtime_id", sa.String(200), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_runtime_work_batch_project_state",
        "runtime_work_batch_projects",
        ["project_id", "state"],
    )


def downgrade():
    raise RuntimeError("Retain pending Work batches; use compatible code rollback")
