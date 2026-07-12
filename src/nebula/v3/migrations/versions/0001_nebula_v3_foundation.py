"""Nebula 3 typed entity store and append-only run ledger.

Revision ID: 0001_nebula_v3
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_nebula_v3"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "schema_versions" not in existing:
        op.create_table(
            "schema_versions",
            sa.Column("version", sa.Integer(), primary_key=True),
            sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.execute(
            sa.text(
                "INSERT INTO schema_versions (version, applied_at) VALUES (2, CURRENT_TIMESTAMP)"
            )
        )
    if "entities" not in existing:
        op.create_table(
            "entities",
            sa.Column("id", sa.String(length=200), primary_key=True),
            sa.Column("kind", sa.String(length=80), nullable=False),
            sa.Column("engagement_id", sa.String(length=200), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_entities_kind", "entities", ["kind"])
        op.create_index(
            "ix_entities_kind_engagement", "entities", ["kind", "engagement_id"]
        )
        op.create_index(
            "ix_entities_engagement_updated",
            "entities",
            ["engagement_id", "updated_at"],
        )
    if "run_events" not in existing:
        op.create_table(
            "run_events",
            sa.Column("id", sa.String(length=200), primary_key=True),
            sa.Column("run_id", sa.String(length=200), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=200), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("actor_id", sa.String(length=200), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("idempotency_key", sa.String(length=300), nullable=True),
            sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
            sa.UniqueConstraint(
                "run_id", "idempotency_key", name="uq_run_events_idempotency"
            ),
        )
        op.create_index("ix_run_events_replay", "run_events", ["run_id", "sequence"])
    if "run_budget_counters" not in existing:
        op.create_table(
            "run_budget_counters",
            sa.Column("run_id", sa.String(length=200), primary_key=True),
            sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "output_tokens", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "cost_microusd", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_run_events_no_update
            BEFORE UPDATE ON run_events
            BEGIN SELECT RAISE(ABORT, 'run events are append-only'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_run_events_no_delete
            BEFORE DELETE ON run_events
            BEGIN SELECT RAISE(ABORT, 'run events are append-only'); END
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_update")
    op.drop_table("run_budget_counters")
    op.drop_table("run_events")
    op.drop_table("entities")
    op.drop_table("schema_versions")
