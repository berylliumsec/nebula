"""Replace only the experimental application-model projection with a project graph."""

from alembic import op
import sqlalchemy as sa

revision = "0014_application_graph"
down_revision = "0013_application_model_outbox"
branch_labels = None
depends_on = None

# Exact allowlist: original browser evidence, observations, and unrelated records
# must survive. Do not broaden this to a LIKE prefix or cascade project deletion.
EXPERIMENTAL_KINDS = (
    "application_model_sessions",
    "application_model_observations",
    "application_model_assertions",
    "application_model_objects",
    "application_model_object_versions",
    "application_model_states",
    "application_model_transitions",
    "application_model_constraints",
    "application_model_queries",
)


def upgrade():
    op.create_table(
        "application_graphs",
        sa.Column(
            "project_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_table(
        "application_graph_edits",
        sa.Column(
            "project_id",
            sa.String(200),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
    )
    op.create_index(
        "uq_application_graph_retry",
        "application_graph_edits",
        ["project_id", "idempotency_key"],
        unique=True,
    )
    entities = sa.table("entities", sa.column("kind"))
    op.get_bind().execute(
        entities.delete().where(entities.c.kind.in_(EXPERIMENTAL_KINDS))
    )
    op.drop_table("application_model_outbox")


def downgrade():
    raise RuntimeError(
        "Experimental graph reset is irreversible; restore a backup to downgrade."
    )
