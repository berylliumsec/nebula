"""Enforce the append-only run-event ledger on PostgreSQL.

Revision ID: 0002_event_immutability
Revises: 0001_nebula_v3
"""

from __future__ import annotations

from alembic import op

revision = "0002_event_immutability"
down_revision = "0001_nebula_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nebula_reject_run_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'run events are append-only';
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_immutable ON run_events")
    op.execute(
        """
        CREATE TRIGGER trg_run_events_immutable
        BEFORE UPDATE OR DELETE ON run_events
        FOR EACH ROW
        EXECUTE FUNCTION nebula_reject_run_event_mutation()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_run_events_immutable ON run_events")
    op.execute("DROP FUNCTION IF EXISTS nebula_reject_run_event_mutation()")
