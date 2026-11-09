"""Add encrypted response references for independently replaceable API workers."""

from alembic import op
import sqlalchemy as sa

revision = "0034_runtime_delivery"
down_revision = "0033_deployment_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_delivery",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("command_id", sa.String(200), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content_reference", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "command_id", "sequence", name="uq_runtime_delivery_sequence"
        ),
    )
    op.create_index(
        "ix_runtime_delivery_project", "runtime_delivery", ["project_id", "command_id"]
    )


def downgrade():
    raise RuntimeError(
        "Retain live command delivery history; use compatible code rollback"
    )
