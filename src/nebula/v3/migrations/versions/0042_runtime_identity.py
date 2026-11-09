"""Retain a nonsecret Core identity across credential and application changes."""

from alembic import op
import sqlalchemy as sa

revision = "0042_runtime_identity"
down_revision = "0041_handoff_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_core_identity",
        sa.Column("key", sa.String(20), primary_key=True),
        sa.Column("core_id", sa.String(200), nullable=False, unique=True),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Retain installation identity; use compatible code rollback")
