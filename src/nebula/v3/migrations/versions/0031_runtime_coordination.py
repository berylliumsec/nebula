"""Add durable process identities, fenced project ownership and command receipts.

This is additive: existing Core versions continue using the same entity and event
tables. Enabling independent workers is a separate rollout step.
"""

from alembic import op
import sqlalchemy as sa

revision = "0031_runtime_coordination"
down_revision = "0030_runtime_switch_lookup"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_instances",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("version", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("heartbeat_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at_ms", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "project_ownership",
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("runtime_id", sa.String(200), sa.ForeignKey("runtime_instances.id")),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("command_sequence", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_project_ownership_runtime", "project_ownership", ["runtime_id"])
    op.create_table(
        "runtime_commands",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("project_id", sa.String(200), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.Integer()),
        sa.Column("accepted_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("runtime_id", sa.String(200)),
        sa.Column("fencing_token", sa.BigInteger()),
        sa.Column("result", sa.JSON()),
        sa.UniqueConstraint(
            "actor_id", "idempotency_key", name="uq_runtime_command_request"
        ),
        sa.UniqueConstraint(
            "project_id", "sequence", name="uq_runtime_command_sequence"
        ),
    )
    op.create_index(
        "ix_runtime_command_dispatch",
        "runtime_commands",
        ["project_id", "state", "sequence"],
    )


def downgrade():
    # Downgrading would destroy fencing counters, receipts, and evidence of
    # uncertain effects. Code rollback must leave this additive schema intact.
    raise RuntimeError(
        "runtime coordination history must be retained during code rollback"
    )
