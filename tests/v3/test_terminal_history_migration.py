from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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
            command.downgrade(config, "0004_zero_setup_bootstrap")
            tables = set(inspect(connection).get_table_names())
            assert "terminal_command_records" not in tables
            assert "terminal_command_preferences" not in tables
    finally:
        engine.dispose()
