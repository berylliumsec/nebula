import json
from pathlib import Path
from types import SimpleNamespace

from nebula import setup_nebula


def build_setup_dialog(tmp_path, monkeypatch, engagement_folder=None):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QDialog { color: white; }")
    monkeypatch.setattr(setup_nebula, "return_path", lambda _: str(stylesheet))
    return setup_nebula.settings(engagement_folder=engagement_folder)


def test_setup_dialog_initial_state_and_toggles(qapp, tmp_path, monkeypatch):
    dialog = build_setup_dialog(tmp_path, monkeypatch)

    try:
        assert dialog.windowTitle() == "Engagement Settings"
        assert dialog.folderPathLabel.text().startswith("You must select")
        assert dialog.saveBtn.isEnabled() is False

        dialog.enableSettings(True)
        assert dialog.saveBtn.isEnabled() is True
        dialog.enableSettings(False)
        assert dialog.saveBtn.isEnabled() is False

        dialog.onModelChanged("deepseek")
        assert dialog.model_name == "deepseek"
    finally:
        dialog.close()


def test_setup_dialog_selects_directories_and_folder(qapp, tmp_path, monkeypatch):
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    dialog = build_setup_dialog(tmp_path, monkeypatch)
    calls = []
    dialog.handle_engagement_selection = lambda: calls.append("handled")
    monkeypatch.setattr(dialog.settings, "value", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr(
        setup_nebula.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(engagement),
    )

    try:
        dialog.selectFolder()
        assert dialog.engagementFolder == str(engagement)
        assert dialog.engagementName == "engagement"
        assert dialog.folderPathLabel.text() == "engagement"
        assert calls == ["handled"]

        dialog.selectChromaDBDir()
        assert dialog.chromadbDir == str(engagement)
        assert dialog.chromadbDirLineEdit.text() == str(engagement)

        dialog.selectthreatDBDir()
        assert dialog.threatdbDir == str(engagement)
        assert dialog.threatdbDirLineEdit.text() == str(engagement)
    finally:
        dialog.close()


def test_setup_dialog_handles_selection_errors(qapp, tmp_path, monkeypatch):
    errors = []
    dialog = build_setup_dialog(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_nebula.logger, "error", errors.append)
    monkeypatch.setattr(
        setup_nebula.QFileDialog,
        "getExistingDirectory",
        lambda *args: (_ for _ in ()).throw(RuntimeError("dialog failed")),
    )

    try:
        dialog.selectFolder()
        dialog.selectChromaDBDir()
        dialog.selectthreatDBDir()
        assert any("dialog failed" in message for message in errors)
    finally:
        dialog.close()


def test_setup_dialog_path_defaults_and_prefill(qapp, tmp_path, monkeypatch):
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    dialog = build_setup_dialog(tmp_path, monkeypatch)

    try:
        dialog.apply_default_paths()
        assert dialog.chromadbDirLineEdit.text() == ""

        dialog.engagementFolder = str(engagement)
        dialog.prefill_paths_with_engagement()
        assert dialog.chromadbDirLineEdit.text() == str(engagement)
        assert dialog.threatdbDirLineEdit.text() == str(engagement)

        dialog.chromadbDirLineEdit.clear()
        dialog.threatdbDirLineEdit.clear()
        dialog.ollamaLineEdit.clear()
        dialog.apply_default_paths()
        assert dialog.chromadbDirLineEdit.text() == str(engagement)
        assert dialog.threatdbDirLineEdit.text() == str(engagement)
        assert dialog.ollamaLineEdit.text() == dialog.default_ollama_url

        recorded = []
        dialog.prefill_paths_with_engagement = lambda: recorded.append("prefill")
        dialog.loadEngagementDetails = lambda: recorded.append("load")
        dialog.apply_default_paths = lambda: recorded.append("apply")
        dialog.enableSettings = lambda enabled: recorded.append(enabled)
        dialog.handle_engagement_selection()
        assert recorded == ["prefill", "load", "apply", True]
    finally:
        dialog.close()


def test_setup_dialog_loads_engagement_details(qapp, tmp_path, monkeypatch):
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    dialog = build_setup_dialog(tmp_path, monkeypatch)

    try:
        assert dialog.loadEngagementDetails() == {}

        dialog.engagementFolder = str(engagement)
        details_path = engagement / "engagement_details.json"
        details_path.write_text(
            json.dumps(
                {
                    "ip_addresses": ["1.1.1.1"],
                    "urls": ["https://example.com"],
                    "lookout_items": ["admin"],
                    "model": "model-a",
                    "chromadb_dir": "/tmp/chroma",
                    "threatdb_dir": "/tmp/threat",
                    "ollama_url": "http://ollama",
                }
            )
        )

        details = dialog.loadEngagementDetails()
        assert details["model"] == "model-a"
        assert dialog.ipAddressesInput.toPlainText() == "1.1.1.1"
        assert dialog.urlsInput.toPlainText() == "https://example.com"
        assert dialog.lookoutInput.toPlainText() == "admin"
        assert dialog.modelLineEdit.text() == "model-a"
        assert dialog.chromadbDirLineEdit.text() == "/tmp/chroma"
        assert dialog.threatdbDirLineEdit.text() == "/tmp/threat"
        assert dialog.ollamaLineEdit.text() == "http://ollama"

        details_path.write_text("{broken")
        assert dialog.loadEngagementDetails() == {}

        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        assert dialog.loadEngagementDetails() == {}
    finally:
        dialog.close()


def test_setup_dialog_validates_and_saves_engagement(qapp, tmp_path, monkeypatch):
    warnings = []
    completed = []
    engagement = tmp_path / "engagement"
    engagement.mkdir()
    dialog = build_setup_dialog(tmp_path, monkeypatch)
    monkeypatch.setattr(
        setup_nebula.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((title, message)),
    )

    try:
        dialog.saveEngagement()
        assert dialog.folderPathLabel.text() == "Please select an engagement folder."

        dialog.engagementFolder = str(engagement)
        dialog.engagementName = "engagement"
        dialog.saveEngagement()
        assert warnings[-1] == ("Input Error", "Please select a ChromaDB directory.")

        dialog.chromadbDirLineEdit.setText("/tmp/chroma")
        dialog.saveEngagement()
        assert warnings[-1] == ("Input Error", "Please select a threatDB directory.")

        dialog.threatdbDirLineEdit.setText("/tmp/threat")
        dialog.saveEngagement()
        assert warnings[-1] == ("Input Error", "Please enter an ollama_model")

        dialog.modelLineEdit.setText("model-a")
        dialog.ipAddressesInput.setText("1.1.1.1\n")
        dialog.urlsInput.setText("https://example.com\n")
        dialog.lookoutInput.setText("admin\n")
        dialog.setupCompleted.connect(completed.append)

        dialog.saveEngagement()

        saved = json.loads((engagement / "engagement_details.json").read_text())
        assert saved == {
            "engagement_name": "engagement",
            "ip_addresses": ["1.1.1.1"],
            "urls": ["https://example.com"],
            "lookout_items": ["admin"],
            "model": "model-a",
            "chromadb_dir": "/tmp/chroma",
            "threatdb_dir": "/tmp/threat",
            "ollama_url": dialog.default_ollama_url,
        }
        assert completed == [str(engagement)]

        monkeypatch.setattr(
            "builtins.open",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
        dialog.saveEngagement()
        assert dialog.folderPathLabel.text() == "Error saving engagement details."
    finally:
        dialog.close()


def test_setup_dialog_init_with_engagement_and_missing_details(
    qapp,
    tmp_path,
    monkeypatch,
):
    engagement = tmp_path / "engagement"
    engagement.mkdir()

    dialog = build_setup_dialog(tmp_path, monkeypatch, engagement_folder=str(engagement))
    blank_dialog = build_setup_dialog(tmp_path, monkeypatch)

    try:
        assert dialog.folderPathLabel.text() == "engagement"
        assert dialog.chromadbDirLineEdit.text() == str(engagement)
        assert dialog.threatdbDirLineEdit.text() == str(engagement)
        assert dialog.loadEngagementDetails() == {}

        blank_dialog.prefill_paths_with_engagement()
        assert blank_dialog.chromadbDirLineEdit.text() == ""
    finally:
        dialog.close()
        blank_dialog.close()
