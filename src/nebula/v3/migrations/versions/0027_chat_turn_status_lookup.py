"""Project active chat-turn status and recovery fields without reading history."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0027_chat_turn_status_lookup"
down_revision = "0026_harness_tool_call_lookup"
branch_labels = None
depends_on = None

INDEX = "ix_entities_chat_turn_status"
PROJECT_INDEX = "ix_entities_project_chat_turn_status"


def _recovery_flags(payload: dict) -> tuple[bool, bool]:
    snapshot = payload.get("request_snapshot")
    recovery = snapshot.get("recovery") if isinstance(snapshot, dict) else None
    if not isinstance(recovery, dict):
        return False, False
    visible = bool(recovery.get("required") or recovery.get("automatic_retry_pending"))
    cause = recovery.get("cause")
    core_resume = (
        recovery.get("required") is True
        and not recovery.get("auto_resume_attempted_at")
        and (
            isinstance(cause, str)
            and cause in {"core_shutdown", "core_restart"}
            or cause is None
            and str(payload.get("error") or "").startswith(
                ("Core stopped ", "Core restarted ")
            )
        )
    )
    return visible, bool(core_resume)


def upgrade() -> None:
    op.add_column("entities", sa.Column("chat_turn_status", sa.String(40)))
    op.add_column("entities", sa.Column("chat_turn_recovery_visible", sa.Boolean()))
    op.add_column("entities", sa.Column("chat_turn_core_resume_pending", sa.Boolean()))
    entities = sa.table(
        "entities",
        sa.column("id", sa.String(200)),
        sa.column("kind", sa.String(80)),
        sa.column("payload", sa.JSON()),
        sa.column("chat_turn_status", sa.String(40)),
        sa.column("chat_turn_recovery_visible", sa.Boolean()),
        sa.column("chat_turn_core_resume_pending", sa.Boolean()),
    )
    connection = op.get_bind()
    status = entities.c.payload["status"]
    if connection.dialect.name == "sqlite":
        is_string = sa.func.json_type(entities.c.payload, '$."status"') == "text"
    else:
        is_string = sa.func.json_typeof(status) == "string"
    connection.execute(
        sa.update(entities)
        .where(entities.c.kind == "chat_turns")
        .values(
            chat_turn_status=sa.case((is_string, status.as_string()), else_=None),
            chat_turn_recovery_visible=False,
            chat_turn_core_resume_pending=False,
        )
    )
    # Only interrupted rows need their recovery metadata parsed. Page the scan
    # so a large existing database never accumulates turn payloads in memory.
    last_id = ""
    while True:
        rows = (
            connection.execute(
                sa.select(entities.c.id, entities.c.payload)
                .where(
                    entities.c.kind == "chat_turns",
                    entities.c.chat_turn_status == "interrupted",
                    entities.c.id > last_id,
                )
                .order_by(entities.c.id)
                .limit(100)
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        for row in rows:
            visible, core_resume = _recovery_flags(row["payload"] or {})
            if visible or core_resume:
                connection.execute(
                    sa.update(entities)
                    .where(entities.c.id == row["id"])
                    .values(
                        chat_turn_recovery_visible=visible,
                        chat_turn_core_resume_pending=core_resume,
                    )
                )
        last_id = rows[-1]["id"]
    op.create_index(
        INDEX,
        "entities",
        [
            "kind",
            "chat_turn_status",
            "engagement_id",
            "created_at",
            "id",
            "chat_session_id",
            "chat_turn_recovery_visible",
            "chat_turn_core_resume_pending",
        ],
    )
    op.create_index(
        PROJECT_INDEX,
        "entities",
        [
            "kind",
            "engagement_id",
            "chat_turn_status",
            "created_at",
            "id",
            "chat_session_id",
            "chat_turn_recovery_visible",
            "chat_turn_core_resume_pending",
        ],
    )


def downgrade() -> None:
    op.drop_index(PROJECT_INDEX, table_name="entities")
    op.drop_index(INDEX, table_name="entities")
    for column in (
        "chat_turn_core_resume_pending",
        "chat_turn_recovery_visible",
        "chat_turn_status",
    ):
        op.drop_column("entities", column)
