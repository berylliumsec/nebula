"""Add the linked memory tables (docs/design/linked-memory.md).

Additive: new tables only. Older code never reads them, and every table is
owned by one project through ``engagement_id``, so the project's execution
worker is the only writer.
"""

from alembic import op
import sqlalchemy as sa

revision = "0052_linked_memory"
down_revision = "0051_storage_open_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "linked_memory_settings",
        sa.Column("engagement_id", sa.String(200), primary_key=True),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.String(50), nullable=False),
    )
    op.create_table(
        "linked_memory_cursors",
        sa.Column("engagement_id", sa.String(200), primary_key=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "linked_memory_goal_runs",
        sa.Column("goal_id", sa.String(200), primary_key=True),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("session_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("position", sa.JSON(), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("mcp_queries", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("created_at", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.String(50), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_linked_memory_goal_runs_state",
        "linked_memory_goal_runs",
        ["engagement_id", "state"],
    )
    op.create_table(
        "linked_memory_cards",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("summary", sa.String(400), nullable=False),
        sa.Column("applies_when", sa.String(300)),
        sa.Column("authority", sa.String(30), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("goal_id", sa.String(200)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("curator_version", sa.String(30), nullable=False),
        sa.Column("curator_model", sa.String(200), nullable=False),
        sa.Column("verified_at", sa.String(50), nullable=False),
        sa.Column("created_at", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.String(50), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "engagement_id", "content_hash", name="uq_linked_memory_card_content"
        ),
    )
    op.create_index(
        "ix_linked_memory_cards_scope",
        "linked_memory_cards",
        ["engagement_id", "state", "updated_at"],
    )
    op.create_table(
        "linked_memory_card_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("card_id", sa.String(64), nullable=False),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("ref", sa.JSON(), nullable=False),
        sa.Column("excerpt", sa.String(500), nullable=False),
        sa.Column("excerpt_hash", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.String(50), nullable=False),
        sa.UniqueConstraint(
            "card_id", "excerpt_hash", name="uq_linked_memory_card_source"
        ),
    )
    op.create_index(
        "ix_linked_memory_card_sources_card_id",
        "linked_memory_card_sources",
        ["card_id"],
    )
    op.create_table(
        "linked_memory_links",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("engagement_id", sa.String(200), nullable=False),
        sa.Column("from_card", sa.String(64), nullable=False),
        sa.Column("to_card", sa.String(64), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("basis", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("curator_version", sa.String(30), nullable=False),
        sa.Column("curator_model", sa.String(200), nullable=False),
        sa.Column("created_at", sa.String(50), nullable=False),
        sa.Column("updated_at", sa.String(50), nullable=False),
        sa.UniqueConstraint(
            "from_card", "to_card", "type", name="uq_linked_memory_link"
        ),
    )
    op.create_index("ix_linked_memory_links_to", "linked_memory_links", ["to_card"])


def downgrade() -> None:
    raise RuntimeError("Keep linked memory cards; use compatible code rollback")
