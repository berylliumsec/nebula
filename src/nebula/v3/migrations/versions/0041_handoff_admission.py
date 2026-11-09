"""Keep the exact admitted work inventory separate from mutable checkpoints."""

from alembic import op
import sqlalchemy as sa

revision = "0041_handoff_admission"
down_revision = "0040_runtime_chat_cursor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "project_handoffs", sa.Column("admitted_turn_ids", sa.JSON(), nullable=True)
    )


def downgrade():
    raise RuntimeError("Retain handoff inventories; use compatible code rollback")
