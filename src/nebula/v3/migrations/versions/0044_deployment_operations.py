"""Retain authenticated management intent independently of supervisor lifetime."""

from alembic import op
import sqlalchemy as sa

revision = "0044_deployment_operations"
down_revision = "0043_runtime_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deployment_operations",
        sa.Column("operation_id", sa.String(200), primary_key=True),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("claim_owner", sa.String(200), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("updated_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_deployment_operation_actor",
        "deployment_operations",
        ["actor_id", "updated_at_ms"],
    )


def downgrade():
    raise RuntimeError("Retain management receipts; use compatible code rollback")
