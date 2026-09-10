"""Keep monotonic session display revisions independent of wall-clock time."""

from alembic import op
import sqlalchemy as sa

revision = "0015_session_projection"
down_revision = "0014_application_graph"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "session_projections",
        sa.Column(
            "session_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.BigInteger, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
    )


def downgrade():
    # This is only a derived cache. No approval, turn or operator data is lost.
    op.drop_table("session_projections")
