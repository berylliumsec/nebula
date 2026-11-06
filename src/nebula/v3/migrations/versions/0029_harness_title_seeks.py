"""Support indexed top-one title sources without ranking historical rows."""

from typing import Any
from alembic import op
import sqlalchemy as sa

revision = "0029_harness_title_seeks"
down_revision = "0028_harness_session_title_lookup"
branch_labels = None
depends_on = None

INDEXES = (
    (
        "ix_entities_harness_title_project",
        ["kind", "harness_session_id", "engagement_id", "created_at", "id"],
        False,
    ),
    (
        "ix_entities_harness_chat_title",
        ["kind", "harness_session_id", "created_at", "id"],
        True,
    ),
    (
        "ix_entities_harness_chat_title_project",
        ["kind", "harness_session_id", "engagement_id", "created_at", "id"],
        True,
    ),
)


def upgrade():
    entities = sa.table(
        "entities", sa.column("kind", sa.String()), sa.column("payload", sa.JSON())
    )
    values = []
    value: Any
    for key in ("temporary_assistant", "side_chat"):
        if op.get_bind().dialect.name == "sqlite":
            value = sa.func.json_extract(entities.c.payload, "$.metadata." + key)
        else:
            value = sa.func.json_extract_path_text(
                entities.c.payload, "metadata", key
            ).cast(sa.Boolean)
        values.append(sa.func.coalesce(value, False).is_(False))
    predicate = sa.and_(entities.c.kind == "chat_sessions", *values)
    for name, columns, partial in INDEXES:
        options: dict[str, Any] = (
            dict(sqlite_where=predicate, postgresql_where=predicate) if partial else {}
        )
        op.create_index(name, "entities", columns, **options)


def downgrade():
    for name, _, _ in reversed(INDEXES):
        op.drop_index(name, table_name="entities")
