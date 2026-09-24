"""Add normalized chat-turn history, checkpoints, and provider admission queue."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017_chat_throughput_foundation"
down_revision = "0016_chat_session_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_turn_step_events",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("turn_id", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("provider_group", sa.Integer()),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("tool_call_id", sa.String(length=200)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.UniqueConstraint("turn_id", "sequence", name="uq_chat_turn_steps_sequence"),
        sa.UniqueConstraint(
            "turn_id", "idempotency_key", name="uq_chat_turn_steps_idempotency"
        ),
    )
    op.create_index(
        "ix_chat_turn_steps_replay", "chat_turn_step_events", ["turn_id", "sequence"]
    )
    op.create_index(
        "ix_chat_turn_steps_step",
        "chat_turn_step_events",
        ["turn_id", "step", "sequence"],
    )
    op.create_table(
        "chat_turn_checkpoints",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("turn_id", sa.String(length=200), nullable=False),
        sa.Column("through_step", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "turn_id", "through_step", name="uq_chat_turn_checkpoint_boundary"
        ),
    )
    op.create_index(
        "ix_chat_turn_checkpoints_latest",
        "chat_turn_checkpoints",
        ["turn_id", "through_step"],
    )
    op.create_table(
        "provider_turn_queue",
        sa.Column("turn_id", sa.String(length=200), primary_key=True),
        sa.Column("lane", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(length=200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_provider_turn_queue_dispatch",
        "provider_turn_queue",
        ["state", "lane", "accepted_at"],
    )
    op.create_index(
        "ix_provider_turn_queue_lease",
        "provider_turn_queue",
        ["state", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("provider_turn_queue")
    op.drop_table("chat_turn_checkpoints")
    op.drop_table("chat_turn_step_events")
