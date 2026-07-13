"""Add the immutable operator-workflow event ledger.

Revision ID: 0003_operation_events
Revises: 0002_event_immutability
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_operation_events"
down_revision = "0002_event_immutability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_events",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("operation_id", sa.String(length=200), nullable=False),
        sa.Column("operation_kind", sa.String(length=80), nullable=False),
        sa.Column("engagement_id", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id", "idempotency_key", name="uq_operation_events_idempotency"
        ),
        sa.UniqueConstraint(
            "operation_id", "sequence", name="uq_operation_events_sequence"
        ),
    )
    op.create_index(
        "ix_operation_events_replay",
        "operation_events",
        ["operation_id", "sequence"],
    )
    op.create_index(
        "ix_operation_events_engagement_time",
        "operation_events",
        ["engagement_id", "occurred_at"],
    )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_no_update
            BEFORE UPDATE ON operation_events
            BEGIN
                SELECT RAISE(ABORT, 'operation events are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_no_delete
            BEFORE DELETE ON operation_events
            BEGIN
                SELECT RAISE(ABORT, 'operation events are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION nebula_reject_operation_event_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'operation events are immutable';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_immutable
            BEFORE UPDATE OR DELETE ON operation_events
            FOR EACH ROW EXECUTE FUNCTION nebula_reject_operation_event_mutation()
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_update")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_operation_events_immutable ON operation_events"
        )
        op.execute("DROP FUNCTION IF EXISTS nebula_reject_operation_event_mutation()")
    op.drop_index("ix_operation_events_engagement_time", table_name="operation_events")
    op.drop_index("ix_operation_events_replay", table_name="operation_events")
    op.drop_table("operation_events")
