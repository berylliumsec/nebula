"""Fence publication of shared derived indexes to committed source digests."""

from alembic import op
import sqlalchemy as sa

revision = "0038_runtime_index_state"
down_revision = "0037_shared_butler_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_index_state",
        sa.Column("source_id", sa.String(200), primary_key=True),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade():
    raise RuntimeError(
        "Retain index publication receipts during compatible code rollback"
    )
