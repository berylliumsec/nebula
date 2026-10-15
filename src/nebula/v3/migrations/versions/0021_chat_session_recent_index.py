"""Index recent conversation pages within one project."""

from alembic import op

revision = "0021_chat_session_recent_index"
down_revision = "0020_default_main_agent_messaging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_entities_kind_engagement_updated_id",
        "entities",
        ["kind", "engagement_id", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_entities_kind_engagement_updated_id", table_name="entities")
