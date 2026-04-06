import json
from pathlib import Path

from nebula import configuration_manager


def test_error_dialog_builds_dark_palette(qapp):
    dialog = configuration_manager.ErrorDialog("broken")

    try:
        assert dialog.windowTitle() == "Error"
        assert dialog.layout().count() == 2
        assert dialog.palette().color(dialog.palette().ColorRole.Window).red() == 53
    finally:
        dialog.close()


def test_config_manager_creates_and_updates_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(configuration_manager.constants, "NEBULA_DIR", str(tmp_path / "nebula-home"))
    monkeypatch.setattr(
        configuration_manager.constants,
        "SYSTEM_LOGS_DIR",
        str(tmp_path / "logs"),
    )

    engagement = tmp_path / "engagement"
    engagement.mkdir()
    (engagement / "engagement_details.json").write_text(
        json.dumps(
            {
                "model": "model-a",
                "chromadb_dir": "/tmp/chroma",
                "threatdb_dir": "/tmp/threat",
                "ollama": True,
                "ollama_url": "http://ollama",
                "use_internet_search": True,
            }
        )
    )

    manager = configuration_manager.ConfigManager(str(engagement))

    config_file = engagement / "config.json"
    saved = json.loads(config_file.read_text())

    assert manager.LOG_DIRECTORY.endswith("command_output")
    assert saved["ENGAGEMENT_FOLDER"] == str(engagement)
    assert saved["MODEL"] == "model-a"
    assert saved["CHROMA_DB_PATH"] == "/tmp/chroma"
    assert saved["THREAT_DB_PATH"] == "/tmp/threat"
    assert saved["OLLAMA"] is True
    assert saved["OLLAMA_URL"] == "http://ollama"
    assert saved["USE_INTERNET_SEARCH"] is True
    assert "AVAILABLE_TOOLS" in saved
    assert Path(saved["LOG_DIRECTORY"]).is_dir()


def test_config_manager_handles_errors_and_helpers(tmp_path, monkeypatch):
    manager = configuration_manager.ConfigManager(None)
    assert manager.CONFIG == {}

    engagement = tmp_path / "engagement"
    engagement.mkdir()
    manager.setengagement_folder(str(engagement))
    assert manager.engagement_folder == str(engagement)

    manager.CONFIG = {"SELECTED_TOOLS": ["nmap"]}
    assert manager.safe_get_selected_tools(manager.CONFIG) == ["nmap"]
    assert manager.safe_get_selected_tools({"SELECTED_TOOLS": "nmap"}) == []
    assert manager.safe_get_selected_tools({}) == []

    class BadDict(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert manager.safe_get_selected_tools(BadDict(SELECTED_TOOLS=["nmap"])) == []

    errors = []
    monkeypatch.setattr(configuration_manager.logger, "error", errors.append)
    monkeypatch.setattr(
        configuration_manager.os,
        "makedirs",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("mkdir failed")),
    )
    manager.create_directory("/tmp/fail")
    assert errors and "mkdir failed" in errors[-1]

    manager.CONFIG_FILE_PATH = str(tmp_path / "missing.json")
    assert manager.load_config() == {}

    manager.CONFIG_FILE_PATH = str(tmp_path / "config.json")
    manager.save_config(manager.CONFIG_FILE_PATH, {"key": "value"})
    assert json.loads(Path(manager.CONFIG_FILE_PATH).read_text()) == {"key": "value"}

    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )
    manager.save_config(manager.CONFIG_FILE_PATH, {"key": "value"})
    assert "write failed" in errors[-1]


def test_config_manager_update_paths_catches_internal_errors(tmp_path, monkeypatch):
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    manager = configuration_manager.ConfigManager(str(engagement))

    errors = []
    monkeypatch.setattr(configuration_manager.logger, "error", errors.append)
    monkeypatch.setattr(manager, "create_directory", lambda directory: (_ for _ in ()).throw(RuntimeError("bad directory")))

    manager.update_paths()

    assert errors and "bad directory" in errors[-1]


def test_config_manager_init_catches_update_path_errors(tmp_path, monkeypatch):
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    debug_messages = []

    monkeypatch.setattr(configuration_manager.logger, "debug", debug_messages.append)
    monkeypatch.setattr(
        configuration_manager.ConfigManager,
        "update_paths",
        lambda self: (_ for _ in ()).throw(RuntimeError("update failed")),
    )

    configuration_manager.ConfigManager(str(engagement))

    assert any("update failed" in message for message in debug_messages)
