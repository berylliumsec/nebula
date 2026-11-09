"""Fence all new runtime admission before a coordinated Core shutdown."""

from alembic import op
import sqlalchemy as sa

revision = "0045_runtime_shutdown"
down_revision = "0044_deployment_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployment_host",
        sa.Column(
            "shutdown_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade():
    raise RuntimeError("Retain shutdown fencing; use compatible code rollback")
