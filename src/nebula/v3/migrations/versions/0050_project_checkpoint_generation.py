"""Track checkpoint input changes, including writes from retained workers."""

from alembic import op
import sqlalchemy as sa

revision = "0050_project_checkpoint_generation"
down_revision = "0049_project_view_leases"
branch_labels = None
depends_on = None


def _projects(table, prefix):
    if table == "entities":
        return (
            f"SELECT {prefix}.engagement_id AS project_id "
            f"UNION SELECT CASE WHEN {prefix}.kind = 'engagements' "
            f"THEN {prefix}.id END AS project_id"
        )
    if table == "provider_turn_queue":
        return (
            "SELECT engagement_id AS project_id FROM entities "
            f"WHERE id = {prefix}.turn_id"
        )
    return f"SELECT {prefix}.project_id AS project_id"


def upgrade():
    op.create_table(
        "project_checkpoint_generations",
        sa.Column("project_id", sa.String(200), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
    )
    postgres = op.get_bind().dialect.name == "postgresql"
    for table in ("entities", "provider_turn_queue", "provider_tool_manifests"):
        for event, prefixes in (
            ("INSERT", ("NEW",)),
            ("UPDATE", ("OLD", "NEW")),
            ("DELETE", ("OLD",)),
        ):
            name = f"checkpoint_generation_{table}_{event.lower()}"
            projects = " UNION ".join(_projects(table, p) for p in prefixes)
            # Sort multi-project changes consistently before touching counters.
            # No FK: deletion must retain the last generation as evidence.
            select_projects = (
                f"SELECT DISTINCT project_id FROM ({projects}) AS affected "
                "WHERE project_id IS NOT NULL ORDER BY project_id"
            )
            if postgres:
                op.execute(
                    f"""CREATE FUNCTION {name}() RETURNS trigger
                    LANGUAGE plpgsql AS $$
                    DECLARE affected_project text;
                    BEGIN
                        FOR affected_project IN {select_projects} LOOP
                            INSERT INTO project_checkpoint_generations
                                (project_id, generation)
                            VALUES (affected_project, 1)
                            ON CONFLICT (project_id) DO UPDATE SET generation =
                                project_checkpoint_generations.generation + 1;
                        END LOOP;
                        RETURN NULL;
                    END $$"""
                )
                op.execute(
                    f"CREATE TRIGGER {name} AFTER {event} ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION {name}()"
                )
            else:
                op.execute(
                    f"""CREATE TRIGGER {name} AFTER {event} ON {table}
                    BEGIN
                        INSERT INTO project_checkpoint_generations
                            (project_id, generation)
                        {select_projects}
                        ON CONFLICT (project_id) DO UPDATE SET generation =
                            project_checkpoint_generations.generation + 1;
                    END"""
                )


def downgrade():
    raise RuntimeError("Retain checkpoint generations; use compatible code rollback")
