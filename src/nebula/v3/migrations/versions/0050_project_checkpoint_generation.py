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


def _statement_projects(table, transition):
    """Collect each affected project once, even for a large bulk write."""
    if table == "entities":
        return (
            f"SELECT engagement_id AS project_id FROM {transition} "
            f"UNION SELECT id AS project_id FROM {transition} "
            "WHERE kind = 'engagements'"
        )
    if table == "provider_turn_queue":
        return (
            "SELECT entity.engagement_id AS project_id "
            f"FROM {transition} AS changed "
            "JOIN entities AS entity ON entity.id = changed.turn_id"
        )
    return f"SELECT project_id FROM {transition}"


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
            if postgres:
                transitions = [p.lower() + "_rows" for p in prefixes]
                projects = " UNION ".join(
                    _statement_projects(table, transition) for transition in transitions
                )
            else:
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
                referencing = " ".join(
                    f"{prefix} TABLE AS {transition}"
                    for prefix, transition in zip(prefixes, transitions)
                )
                op.execute(
                    f"CREATE TRIGGER {name} AFTER {event} ON {table} "
                    f"REFERENCING {referencing} "
                    f"FOR EACH STATEMENT EXECUTE FUNCTION {name}()"
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
