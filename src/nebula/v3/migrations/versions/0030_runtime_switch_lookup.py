"""Index pending runtime selection discovery across Core processes."""

from alembic import op
import sqlalchemy as sa

revision = "0030_runtime_switch_lookup"
down_revision = "0029_harness_title_seeks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("entities", sa.Column("runtime_switch_status", sa.String(40)))
    entities = sa.table(
        "entities",
        sa.column("kind", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("runtime_switch_status", sa.String(40)),
    )
    value = entities.c.payload["metadata"]["pending_runtime_switch"]["status"]
    if op.get_bind().dialect.name == "sqlite":
        valid = (
            sa.func.json_type(
                entities.c.payload, "$.metadata.pending_runtime_switch.status"
            )
            == "text"
        )
    else:
        valid = sa.func.json_typeof(value) == "string"
    op.get_bind().execute(
        sa.update(entities)
        .where(entities.c.kind == "chat_sessions")
        .values(runtime_switch_status=sa.case((valid, value.as_string()), else_=None))
    )
    op.create_index(
        "ix_entities_runtime_switch",
        "entities",
        ["kind", "runtime_switch_status", "created_at", "id"],
    )


def downgrade():
    op.drop_index("ix_entities_runtime_switch", table_name="entities")
    op.drop_column("entities", "runtime_switch_status")
