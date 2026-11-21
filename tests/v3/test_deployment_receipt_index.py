"""The supervisor finds receipts by action without reading the whole table."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from nebula.v3.database import Database, DeploymentReceiptRow
from tests.v3.test_runtime_coordination import location as location

INDEX = "ix_deployment_receipt_action_time"


def _migrate(engine, revision, *, down=False):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[2] / "src/nebula/v3/migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def _latest_promotion():
    # The shape RuntimeAutoAdmission.reconcile reads on every pass.
    return (
        select(DeploymentReceiptRow.id)
        .where(DeploymentReceiptRow.action == "api.promoted")
        .order_by(
            DeploymentReceiptRow.result["revision"].as_integer().desc().nullslast(),
            DeploymentReceiptRow.occurred_at_ms.desc(),
            DeploymentReceiptRow.id.desc(),
        )
        .limit(1)
    )


def test_receipts_saved_before_the_index_are_found_by_action(location):
    engine = create_engine(
        Database._normalize_sync_url(location), pool_size=1, max_overflow=0
    )
    try:
        _migrate(engine, "0054_memory_job_labels")
        receipts = Table("deployment_receipts", MetaData(), autoload_with=engine)
        # A long-lived Core's history: many churn receipts, few promotions.
        rows = [
            dict(
                id=f"churn-{index:05d}",
                action="project.retirement_requested",
                result={"project_id": f"p{index % 7}", "reason": "idle"},
                occurred_at_ms=index,
            )
            for index in range(20_000)
        ] + [
            dict(
                id=f"promoted-{revision}",
                action="api.promoted",
                result={"revision": revision},
                occurred_at_ms=100_000 + revision,
            )
            for revision in range(1, 4)
        ]
        with engine.begin() as connection:
            for offset in range(0, len(rows), 2000):
                connection.execute(receipts.insert(), rows[offset : offset + 2000])

        _migrate(engine, "0055_deployment_receipt_action")
        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(engine).get_indexes("deployment_receipts")
        }
        assert indexes[INDEX] == ["action", "occurred_at_ms"]
        with engine.connect() as connection:
            assert connection.scalar(_latest_promotion()) == "promoted-3"
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql("ANALYZE deployment_receipts")
                compiled = _latest_promotion().compile(
                    engine, compile_kwargs={"literal_binds": True}
                )
                plan = "\n".join(
                    connection.execute(text(f"EXPLAIN {compiled}")).scalars()
                )
                assert INDEX in plan
                assert "Seq Scan on deployment_receipts" not in plan

        # Code rollback needs nothing; a schema downgrade drops only the index.
        _migrate(engine, "0054_memory_job_labels", down=True)
        names = {i["name"] for i in inspect(engine).get_indexes("deployment_receipts")}
        assert INDEX not in names
        with engine.connect() as connection:
            assert connection.scalar(_latest_promotion()) == "promoted-3"
    finally:
        engine.dispose()
