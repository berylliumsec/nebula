"""Add an indexed exact-conversation projection for chat-owned records."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_chat_session_lookup"
down_revision = "0015_session_projection"
branch_labels = None
depends_on = None


_SESSION_ID_KINDS = frozenset(
    {
        "chat_bookmarks",
        "chat_decisions",
        "chat_goals",
        "chat_messages",
        "chat_queues",
        "chat_read_cursors",
        "chat_schedules",
        "chat_turns",
    }
)

_CHAT_SESSION_ID_KINDS = frozenset(
    {
        "approvals",
        "harness_interactions",
        "harness_turns",
        "native_checkpoints",
        "native_hook_executions",
        "tool_calls",
    }
)


def _backfill_projection() -> None:
    entities = sa.table(
        "entities",
        sa.column("id", sa.String(200)),
        sa.column("kind", sa.String(80)),
        sa.column("payload", sa.JSON()),
        sa.column("chat_session_id", sa.String(200)),
    )
    connection = op.get_bind()
    kinds = sorted(_SESSION_ID_KINDS | _CHAT_SESSION_ID_KINDS)
    last_id = ""
    while True:
        rows = (
            connection.execute(
                sa.select(entities.c.id, entities.c.kind, entities.c.payload)
                .where(entities.c.kind.in_(kinds), entities.c.id > last_id)
                .order_by(entities.c.id)
                .limit(1_000)
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        for row in rows:
            payload = row["payload"] or {}
            field = (
                "session_id" if row["kind"] in _SESSION_ID_KINDS else "chat_session_id"
            )
            session_id = payload.get(field)
            if isinstance(session_id, str) and session_id:
                connection.execute(
                    sa.update(entities)
                    .where(entities.c.id == row["id"])
                    .values(chat_session_id=session_id)
                )
        last_id = rows[-1]["id"]


def upgrade() -> None:
    with op.batch_alter_table("entities") as batch:
        batch.add_column(sa.Column("chat_session_id", sa.String(length=200)))
    _backfill_projection()
    op.create_index(
        "ix_entities_kind_chat_session_created",
        "entities",
        ["kind", "chat_session_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_entities_kind_chat_session_created", table_name="entities")
    with op.batch_alter_table("entities") as batch:
        batch.drop_column("chat_session_id")
