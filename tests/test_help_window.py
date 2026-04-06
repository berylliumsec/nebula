from PyQt6.QtWidgets import QTextBrowser

from nebula import help as help_module


def test_help_window_builds_help_content(qapp, monkeypatch):
    monkeypatch.setattr(
        help_module.update_utils,
        "return_path",
        lambda path: f"/assets/{path}",
    )

    window = help_module.HelpWindow()

    try:
        browser = window.findChild(QTextBrowser)

        assert browser is not None
        assert "Nebula: Help and User Guide" == window.windowTitle()
        assert "Nebula: AI-Driven PenTestOps Platform" in browser.toHtml()
        assert "/assets/Images_readme/ai_notes.png" in window.get_help_content()
    finally:
        window.close()
