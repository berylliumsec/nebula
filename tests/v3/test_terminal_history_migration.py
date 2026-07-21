from __future__ import annotations

import hashlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _config(connection) -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    config.attributes["connection"] = connection
    return config


def test_terminal_history_migrates_forward_and_back_from_bootstrap_v4(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'terminal-history-migration.db'}",
        future=True,
    )
    try:
        with engine.connect() as connection:
            config = _config(connection)
            command.upgrade(config, "0004_zero_setup_bootstrap")
            tables = set(inspect(connection).get_table_names())
            assert "terminal_command_records" not in tables
            assert "terminal_command_preferences" not in tables

            command.upgrade(config, "0005_terminal_command_history")
            tables = set(inspect(connection).get_table_names())
            assert "terminal_command_records" in tables
            assert "terminal_command_preferences" in tables
            connection.execute(
                text(
                    "INSERT INTO terminal_command_records "
                    "(id, engagement_id, session_id, command, cwd, exit_code, occurred_at) "
                    "VALUES ('legacy-command', 'legacy-project', 'legacy-session', "
                    "'whoami', '/workspace', 0, '2026-01-01 00:00:00')"
                )
            )
            connection.commit()

            command.upgrade(config, "0006_terminal_command_audit")
            migrated = (
                connection.execute(
                    text(
                        "SELECT operator_id, command_sha256, status, "
                        "raw_output_artifact_id, capture_error "
                        "FROM terminal_command_records WHERE id = 'legacy-command'"
                    )
                )
                .mappings()
                .one()
            )
            assert migrated["operator_id"] is None
            assert migrated["command_sha256"] == hashlib.sha256(b"whoami").hexdigest()
            assert migrated["status"] == "legacy_metadata_only"
            assert migrated["raw_output_artifact_id"] is None
            assert "not captured" in migrated["capture_error"]

            command.upgrade(config, "0007_selective_terminal_output")
            selective = (
                connection.execute(
                    text(
                        "SELECT capture_decision, matched_tools, "
                        "recording_policy_revision, runtime_image_digest "
                        "FROM terminal_command_records WHERE id = 'legacy-command'"
                    )
                )
                .mappings()
                .one()
            )
            assert dict(selective) == {
                "capture_decision": "legacy_metadata_only",
                "matched_tools": "[]",
                "recording_policy_revision": None,
                "runtime_image_digest": None,
            }
            preference_columns = {
                item["name"]
                for item in inspect(connection).get_columns(
                    "terminal_command_preferences"
                )
            }
            assert {"custom_tools", "disabled_tools", "revision"} <= preference_columns

            command.downgrade(config, "0006_terminal_command_audit")
            assert "capture_decision" not in {
                item["name"]
                for item in inspect(connection).get_columns("terminal_command_records")
            }
            command.downgrade(config, "0005_terminal_command_history")
            assert (
                connection.execute(
                    text(
                        "SELECT command FROM terminal_command_records "
                        "WHERE id = 'legacy-command'"
                    )
                ).scalar_one()
                == "whoami"
            )
            command.downgrade(config, "0004_zero_setup_bootstrap")
            tables = set(inspect(connection).get_table_names())
            assert "terminal_command_records" not in tables
            assert "terminal_command_preferences" not in tables
    finally:
        engine.dispose()
