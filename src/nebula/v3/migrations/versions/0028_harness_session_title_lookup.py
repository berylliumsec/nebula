"""Index harness-session links for bounded display-name enrichment."""

from alembic import op
import sqlalchemy as sa

revision = "0028_harness_session_title_lookup"
down_revision = "0027_chat_turn_status_lookup"
branch_labels = None
depends_on = None

INDEX = "ix_entities_kind_harness_session_created"
COLUMN = "harness_session_id"
KINDS = ("chat_sessions", "runs", "harness_turns")


def upgrade() -> None:
    op.add_column("entities", sa.Column(COLUMN, sa.String(200), nullable=True))
    entities = sa.table(
        "entities",
        sa.column("kind", sa.String(80)),
        sa.column("payload", sa.JSON()),
        sa.column(COLUMN, sa.String(200)),
    )
    connection = op.get_bind()
    value = entities.c.payload["harness_session_id"]
    if connection.dialect.name == "sqlite":
        is_string = (
            sa.func.json_type(entities.c.payload, '$."harness_session_id"') == "text"
        )
    else:
        is_string = sa.func.json_typeof(value) == "string"
    connection.execute(
        sa.update(entities)
        .where(entities.c.kind.in_(KINDS))
        .values(harness_session_id=sa.case((is_string, value.as_string()), else_=None))
    )
    op.create_index(INDEX, "entities", ["kind", COLUMN, "created_at", "id"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="entities")
    op.drop_column("entities", COLUMN)
