"""Index the storage rows that still need work, not their settled history.

Index-only and additive: no column or payload changes, so older code keeps
reading and writing the same rows, and the database maintains the indexes for
every writer.
"""

from typing import Any

from alembic import op
import sqlalchemy as sa

revision = "0049_storage_open_rows"
down_revision = "0048_chat_scoped_admission"
branch_labels = None
depends_on = None

# (index, kind, payload field, values, values-are-open)
INDEXES = (
    (
        "ix_entities_storage_dispatches_open",
        "storage_dispatches",
        "status",
        ("pending",),
        True,
    ),
    (
        "ix_entities_storage_incidents_open",
        "storage_incidents",
        "state",
        ("resolved",),
        False,
    ),
    (
        "ix_entities_storage_actions_open",
        "storage_actions",
        "status",
        ("expired", "complete", "failed"),
        False,
    ),
)


def upgrade():
    entities = sa.table(
        "entities", sa.column("kind", sa.String()), sa.column("payload", sa.JSON())
    )
    sqlite = op.get_bind().dialect.name == "sqlite"
    for name, kind, field, values, open_values in INDEXES:
        value: Any = (
            sa.func.json_extract(entities.c.payload, "$." + field)
            if sqlite
            else sa.func.json_extract_path_text(entities.c.payload, field)
        )
        if len(values) == 1:
            test = value == values[0] if open_values else value != values[0]
        else:
            test = value.in_(values) if open_values else value.not_in(values)
        predicate = sa.and_(entities.c.kind == kind, test)
        op.create_index(
            name,
            "entities",
            ["kind", "created_at", "id"],
            sqlite_where=predicate,
            postgresql_where=predicate,
        )


def downgrade():
    for name, *_ in reversed(INDEXES):
        op.drop_index(name, table_name="entities")
