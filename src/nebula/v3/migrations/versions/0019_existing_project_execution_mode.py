"""Preserve Docker for projects created before Host became the default."""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa

revision = "0019_existing_project_execution_mode"
down_revision = "0018_event_owner_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Alembic creates version_num as VARCHAR(32), but this and the next
        # existing revision IDs are longer. Widen before Alembic records 0019.
        op.execute(
            "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"
        )
    entities = sa.Table("entities", sa.MetaData(), autoload_with=bind)
    existing = set(
        bind.execute(
            sa.select(entities.c.engagement_id).where(
                entities.c.kind == "automation_policies"
            )
        ).scalars()
    )
    project_ids = list(
        bind.execute(
            sa.select(entities.c.id).where(entities.c.kind == "engagements")
        ).scalars()
    )
    now = datetime.now(timezone.utc)
    for engagement_id in project_ids:
        policy_id = str(
            uuid5(NAMESPACE_URL, f"nebula:automation-policy:{engagement_id}")
        )
        if engagement_id in existing:
            continue
        payload = {
            "id": policy_id,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "revision": 1,
            "execution_mode": "docker",
            "host_access_acknowledged": False,
            "engagement_id": engagement_id,
            "approval_policy": "on_boundary",
            "browser_security_allow_all": False,
            "network_enabled": True,
            "runner_profile_id": None,
            "vpn_profile_id": None,
            "max_timeout_ms": 300_000,
            "metadata": {},
        }
        bind.execute(
            entities.insert().values(
                id=policy_id,
                kind="automation_policies",
                engagement_id=engagement_id,
                revision=1,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    # These policies may have been edited after upgrade; keep operator choices.
    pass
