"""Keep one-use pairing offers across API replacement without credential storage."""

from alembic import op
import sqlalchemy as sa

revision = "0035_device_pairing_offers"
down_revision = "0034_runtime_delivery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_pairing_offers",
        sa.Column("token_sha256", sa.String(64), primary_key=True),
        sa.Column("confirmation_mac", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_device_pairing_offers_expires_at_ms",
        "device_pairing_offers",
        ["expires_at_ms"],
    )


def downgrade():
    raise RuntimeError("Keep active pairing offers; use compatible code rollback")
