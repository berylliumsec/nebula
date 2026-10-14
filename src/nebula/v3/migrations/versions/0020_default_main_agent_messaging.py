"""Enable messaging in legacy project defaults without rewriting conversations."""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0020_default_main_agent_messaging"
down_revision = "0019_existing_project_execution_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    entities = sa.Table("entities", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(
        sa.select(entities.c.id, entities.c.revision, entities.c.payload).where(
            entities.c.kind == "engagements"
        )
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        payload = dict(row.payload)
        defaults = dict(payload.get("assistant_defaults") or {})
        if defaults.get("allow_agent_messaging") is True:
            continue
        defaults["allow_agent_messaging"] = True
        payload["assistant_defaults"] = defaults
        payload["revision"] = row.revision + 1
        payload["updated_at"] = now.isoformat()
        bind.execute(
            entities.update()
            .where(entities.c.id == row.id, entities.c.revision == row.revision)
            .values(revision=row.revision + 1, updated_at=now, payload=payload)
        )


def downgrade() -> None:
    # A project default may have been edited since upgrade; preserve its choice.
    pass
