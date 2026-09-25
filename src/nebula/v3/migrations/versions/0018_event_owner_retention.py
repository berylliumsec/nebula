"""Let event history go with the record that owns it.

Run and operation events stay append-only while their owner exists: updates
are always refused, and so is a delete while the owning record is stored. Once
the owner is gone its history may be deleted. A run event's owner is the
record whose id is its ``run_id`` (a Mission, chat turn or harness turn). An
operation event's owners are its project and, for the kinds listed below, the
record whose id is its ``operation_id``; other kinds keep their events until
their project is deleted.

Revision ID: 0018_event_owner_retention
Revises: 0017_chat_throughput_foundation
"""

from __future__ import annotations

from alembic import op

revision = "0018_event_owner_retention"
down_revision = "0017_chat_throughput_foundation"
branch_labels = None
depends_on = None

# Frozen copy of nebula.v3.event_history.OPERATION_EVENT_OWNER_KINDS.
_OWNED_OPERATION_KINDS = (
    "action_intent",
    "artifact",
    "browser_action",
    "browser_actions",
    "browser_attack",
    "browser_attack_results",
    "browser_attacks",
    "browser_automation_lease",
    "browser_automation_leases",
    "browser_command",
    "browser_commands",
    "browser_crawl",
    "browser_crawl_jobs",
    "browser_handoff",
    "browser_handoffs",
    "browser_identities",
    "browser_intercept",
    "browser_intercepts",
    "browser_proxy_rule",
    "browser_proxy_rules",
    "browser_repeater_results",
    "browser_repeater_tab",
    "browser_repeater_tabs",
    "browser_session",
    "browser_sessions",
    "browser_site_edges",
    "browser_site_node",
    "browser_site_nodes",
    "browser_token_analyses",
    "browser_traffic",
    "browser_websocket_frames",
    "execution",
    "findings",
    "handoff",
    "harness_turn",
    "report_render",
    "reports",
)
_KIND_LIST = ", ".join(f"'{kind}'" for kind in _OWNED_OPERATION_KINDS)

# True while an operation event's history must be kept.
_OPERATION_OWNER_EXISTS = f"""
    EXISTS (
        SELECT 1 FROM entities
        WHERE id = OLD.engagement_id AND kind = 'engagements'
    )
    AND (
        OLD.operation_kind NOT IN ({_KIND_LIST})
        OR EXISTS (SELECT 1 FROM entities WHERE id = OLD.operation_id)
    )
"""
_RUN_OWNER_EXISTS = "EXISTS (SELECT 1 FROM entities WHERE id = OLD.run_id)"
_OPERATION_REFUSAL = "operation events are immutable while their record exists"
_RUN_REFUSAL = "run events are append-only while their record exists"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_delete")
        op.execute(
            f"""
            CREATE TRIGGER trg_operation_events_no_delete
            BEFORE DELETE ON operation_events
            WHEN {_OPERATION_OWNER_EXISTS}
            BEGIN
                SELECT RAISE(ABORT, '{_OPERATION_REFUSAL}');
            END
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_delete")
        op.execute(
            f"""
            CREATE TRIGGER trg_run_events_no_delete
            BEFORE DELETE ON run_events
            WHEN {_RUN_OWNER_EXISTS}
            BEGIN
                SELECT RAISE(ABORT, '{_RUN_REFUSAL}');
            END
            """
        )
    elif dialect == "postgresql":
        # Updates keep the original rejecting triggers; deletes get their own.
        op.execute(
            "DROP TRIGGER IF EXISTS trg_operation_events_immutable ON operation_events"
        )
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_immutable
            BEFORE UPDATE ON operation_events
            FOR EACH ROW EXECUTE FUNCTION nebula_reject_operation_event_mutation()
            """
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION nebula_guard_operation_event_delete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF {_OPERATION_OWNER_EXISTS} THEN
                    RAISE EXCEPTION '{_OPERATION_REFUSAL}';
                END IF;
                RETURN OLD;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_owner_delete
            BEFORE DELETE ON operation_events
            FOR EACH ROW EXECUTE FUNCTION nebula_guard_operation_event_delete()
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_immutable ON run_events")
        op.execute(
            """
            CREATE TRIGGER trg_run_events_immutable
            BEFORE UPDATE ON run_events
            FOR EACH ROW EXECUTE FUNCTION nebula_reject_run_event_mutation()
            """
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION nebula_guard_run_event_delete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF {_RUN_OWNER_EXISTS} THEN
                    RAISE EXCEPTION '{_RUN_REFUSAL}';
                END IF;
                RETURN OLD;
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_run_events_owner_delete
            BEFORE DELETE ON run_events
            FOR EACH ROW EXECUTE FUNCTION nebula_guard_run_event_delete()
            """
        )


def downgrade() -> None:
    # Restores the unconditional refusal. History already deleted stays gone.
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_operation_events_no_delete")
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_no_delete
            BEFORE DELETE ON operation_events
            BEGIN
                SELECT RAISE(ABORT, 'operation events are immutable');
            END
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_no_delete")
        op.execute(
            """
            CREATE TRIGGER trg_run_events_no_delete
            BEFORE DELETE ON run_events
            BEGIN SELECT RAISE(ABORT, 'run events are append-only'); END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_operation_events_owner_delete ON operation_events"
        )
        op.execute("DROP FUNCTION IF EXISTS nebula_guard_operation_event_delete()")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_operation_events_immutable ON operation_events"
        )
        op.execute(
            """
            CREATE TRIGGER trg_operation_events_immutable
            BEFORE UPDATE OR DELETE ON operation_events
            FOR EACH ROW EXECUTE FUNCTION nebula_reject_operation_event_mutation()
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_owner_delete ON run_events")
        op.execute("DROP FUNCTION IF EXISTS nebula_guard_run_event_delete()")
        op.execute("DROP TRIGGER IF EXISTS trg_run_events_immutable ON run_events")
        op.execute(
            """
            CREATE TRIGGER trg_run_events_immutable
            BEFORE UPDATE OR DELETE ON run_events
            FOR EACH ROW EXECUTE FUNCTION nebula_reject_run_event_mutation()
            """
        )
