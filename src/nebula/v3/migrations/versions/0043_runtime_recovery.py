"""Retain exact ownership recovery operations and their observed outcomes."""

from alembic import op
import sqlalchemy as sa

revision = "0043_runtime_recovery"
down_revision = "0042_runtime_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_recoveries",
        sa.Column("operation_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_runtime_id", sa.String(200), nullable=True),
        sa.Column("source_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("target_runtime_id", sa.String(200), nullable=False),
        sa.Column("target_fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("expected_handoff_revision", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_runtime_recovery_project", "runtime_recoveries", ["project_id"])


def downgrade():
    raise RuntimeError("Retain recovery evidence; use compatible code rollback")
