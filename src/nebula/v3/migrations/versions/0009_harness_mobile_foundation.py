"""Index additive harness, media, lineage, and paired-device entities.

Revision ID: 0009_harness_mobile_foundation
Revises: 0008_artifact_query_budget

The new records use Nebula's versioned JSON entity envelope, so their fields do
not require destructive table rewrites. This index keeps kind-scoped replay and
device-session lookup bounded as those entity families grow.
"""

from __future__ import annotations

from alembic import op

revision = "0009_harness_mobile_foundation"
down_revision = "0008_artifact_query_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_entities_kind_updated", "entities", ["kind", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_entities_kind_updated", table_name="entities")
