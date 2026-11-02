"""Index tool artifacts, tool-call recovery, and stable project pages.

Nullable columns are added in place. Backfill JSON strings with set-based SQL,
then build indexes once; the authoritative envelopes and revisions are untouched.
Rehearse on a verified backup on healthy storage before production startup.
"""

from __future__ import annotations

from typing import Any

from alembic import op
import sqlalchemy as sa

revision = "0024_entity_hot_lookup_indexes"
down_revision = "0023_workspace_provenance_ledger"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_entities_kind_engagement_created_id": [
        "kind",
        "engagement_id",
        "created_at",
        "id",
    ],
    "ix_entities_artifact_tool_call_created": [
        "kind",
        "engagement_id",
        "artifact_tool_call_id",
        "created_at",
        "id",
    ],
    "ix_entities_tool_call_recovery_created": [
        "kind",
        "tool_call_status",
        "tool_call_origin",
        "created_at",
        "id",
    ],
}


def upgrade() -> None:
    for name, length in (
        ("artifact_tool_call_id", 200),
        ("tool_call_status", 40),
        ("tool_call_origin", 40),
    ):
        op.add_column("entities", sa.Column(name, sa.String(length), nullable=True))
    entities = sa.table(
        "entities",
        sa.column("kind", sa.String(80)),
        sa.column("payload", sa.JSON()),
        sa.column("artifact_tool_call_id", sa.String(200)),
        sa.column("tool_call_status", sa.String(40)),
        sa.column("tool_call_origin", sa.String(40)),
    )
    connection = op.get_bind()

    def string_projection(*path: str) -> sa.ColumnElement[Any]:
        value: Any = entities.c.payload
        for part in path:
            value = value[part]
        if connection.dialect.name == "sqlite":
            is_string = (
                sa.func.json_type(
                    entities.c.payload, "$" + "".join(f'."{part}"' for part in path)
                )
                == "text"
            )
        else:
            is_string = sa.func.json_typeof(value) == "string"
        return sa.case((is_string, value.as_string()), else_=None)

    connection.execute(
        sa.update(entities)
        .where(entities.c.kind == "artifacts")
        .values(artifact_tool_call_id=string_projection("metadata", "tool_call_id"))
    )
    connection.execute(
        sa.update(entities)
        .where(entities.c.kind == "tool_calls")
        .values(
            tool_call_status=string_projection("status"),
            tool_call_origin=string_projection("origin"),
        )
    )
    for name, columns in _INDEXES.items():
        op.create_index(name, "entities", columns)


def downgrade() -> None:
    for name in reversed(_INDEXES):
        op.drop_index(name, table_name="entities")
    # Native DROP COLUMN preserves the table referenced by event-ledger triggers.
    # A SQLite batch copy temporarily drops it and invalidates those triggers.
    for column in ("tool_call_origin", "tool_call_status", "artifact_tool_call_id"):
        op.drop_column("entities", column)
