"""Seek to a harness turn's active tool calls without reading historical payloads."""

from alembic import op
import sqlalchemy as sa

revision = "0026_harness_tool_call_lookup"
down_revision = "0025_merge_project_and_lookups"
branch_labels = None
depends_on = None

INDEX = "ix_entities_tool_call_turn_status_created"
COLUMN = "tool_call_harness_turn_id"


def upgrade() -> None:
    op.add_column("entities", sa.Column(COLUMN, sa.String(200), nullable=True))
    entities = sa.table(
        "entities",
        sa.column("kind", sa.String(80)),
        sa.column("payload", sa.JSON()),
        sa.column(COLUMN, sa.String(200)),
    )
    connection = op.get_bind()
    value = entities.c.payload["metadata"]["harness_turn_id"]
    if connection.dialect.name == "sqlite":
        is_string = (
            sa.func.json_type(entities.c.payload, '$."metadata"."harness_turn_id"')
            == "text"
        )
    else:
        is_string = sa.func.json_typeof(value) == "string"
    connection.execute(
        sa.update(entities)
        .where(entities.c.kind == "tool_calls")
        .values(
            tool_call_harness_turn_id=sa.case(
                (is_string, value.as_string()), else_=None
            )
        )
    )
    op.create_index(
        INDEX,
        "entities",
        ["kind", COLUMN, "engagement_id", "tool_call_status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="entities")
    # Native DROP COLUMN leaves the append-only event triggers intact.
    op.drop_column("entities", COLUMN)
