"""Share runtime admission capacity across resident execution versions."""

from alembic import op
import sqlalchemy as sa

revision = "0036_runtime_capacity"
down_revision = "0035_device_pairing_offers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_capacity_policy",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
    )
    op.create_table(
        "runtime_capacity_reservations",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("demands", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("released_at_ms", sa.BigInteger()),
        sa.Column("release_reason", sa.String(100)),
        sa.UniqueConstraint(
            "runtime_id",
            "project_id",
            "fencing_token",
            "request_key",
            name="uq_runtime_capacity_request",
        ),
    )
    op.create_index(
        "ix_runtime_capacity_state",
        "runtime_capacity_reservations",
        ["state", "runtime_id"],
    )


def downgrade():
    raise RuntimeError(
        "Resident execution keeps its capacity ledger during code rollback"
    )
