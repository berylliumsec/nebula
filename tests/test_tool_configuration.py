from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QPoint

from nebula import tool_configuration


def build_tools_window(qapp, tmp_path):
    updates = []
    additions = []
    window = tool_configuration.ToolsWindow(
        available_tools=["nmap", "burp"],
        selected_tools=["nmap"],
        icons_path=str(tmp_path),
        update_callback=lambda tools: updates.append(list(tools)),
        add_tool_callback=additions.append,
    )
    return window, updates, additions


def test_tools_window_selection_and_updates(qapp, tmp_path, monkeypatch):
    window, updates, additions = build_tools_window(qapp, tmp_path)
    infos = []
    warnings = []
    scrolled = []

    monkeypatch.setattr(
        tool_configuration.QMessageBox,
        "information",
        lambda parent, title, message: infos.append((title, message)),
    )
    monkeypatch.setattr(
        tool_configuration.QMessageBox,
        "warning",
        lambda parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(window, "scrollToButton", lambda button: scrolled.append(button.text()))
    monkeypatch.setattr(
        tool_configuration.QTimer,
        "singleShot",
        lambda delay, callback: callback(),
    )

    try:
        assert window.windowTitle() == "Select Tools"
        assert window.buttons["nmap"].isChecked() is True
        assert window.buttons["burp"].isChecked() is False

        window.select_all_tools()
        assert set(window.selected_tools) == {"nmap", "burp"}

        window.deselect_all_tools()
        assert window.selected_tools == []

        window.tool_selection_changed("nmap", True)
        window.tool_selection_changed("nmap", False)
        assert updates[-1] == []

        window.tool_name_input.setText("zmap")
        window.add_tool()
        assert additions == ["zmap"]
        assert infos == [("Tool Added", "'zmap' has been added successfully.")]

        window.available_tools.append("zmap")
        window.tool_name_input.setText("zmap")
        window.add_tool()
        assert warnings == [("Tool Exists", "The tool 'zmap' already exists.")]

        window.update_config(["burp", "nmap"], ["burp"])
        assert window.selected_tools == ["burp"]
        assert window.buttons["burp"].isChecked() is True

        action = window.select_all_button
        window.change_icon_temporarily(action, str(tmp_path / "temp.png"), str(tmp_path / "orig.png"))
        window.provide_feedback_and_execute(
            action,
            str(tmp_path / "temp.png"),
            str(tmp_path / "orig.png"),
            lambda: additions.append("executed"),
        )
        assert additions[-1] == "executed"

        window.search_tool("map")
        assert scrolled == ["nmap"]
        window.search_tool("missing")
        assert window.buttons["burp"].styleSheet() == ""

        target_button = window.buttons["burp"]
        target_button.move(0, 100)
        window.scrollToButton(target_button)
        assert window.scroll_area.verticalScrollBar().value() <= 100
    finally:
        window.close()


def test_tools_window_branch_paths(qapp, tmp_path, monkeypatch):
    updates = []
    window = tool_configuration.ToolsWindow(
        available_tools=["a", "b", "c", "d", "e"],
        selected_tools=[],
        icons_path=str(tmp_path),
        update_callback=lambda tools: updates.append(list(tools)),
        add_tool_callback=lambda name: None,
    )

    try:
        assert window.grid_layout.count() == 5
        window.refresh_tools_grid()
        assert "e" in window.buttons

        class FakeButton:
            def __init__(self, checked):
                self.checked = checked

            def isChecked(self):
                return self.checked

            def setChecked(self, checked):
                self.checked = checked

        window.buttons = {
            "alpha": FakeButton(False),
            "beta": FakeButton(True),
        }
        window.selected_tools = []
        window.select_all_tools()
        assert window.selected_tools == ["alpha"]

        window.buttons = {
            "alpha": FakeButton(True),
            "beta": FakeButton(True),
        }
        window.selected_tools = ["alpha", "beta"]
        window.deselect_all_tools()
        assert window.selected_tools == []

        scrollbar_values = []
        window.scroll_area = SimpleNamespace(
            viewport=lambda: SimpleNamespace(height=lambda: 100),
            verticalScrollBar=lambda: SimpleNamespace(setValue=scrollbar_values.append),
        )
        fake_button = SimpleNamespace(pos=lambda: QPoint(0, 250))
        window.scrollToButton(fake_button)
        assert scrollbar_values == [200]
    finally:
        window.close()
