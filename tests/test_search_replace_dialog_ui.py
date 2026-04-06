from PyQt6.QtWidgets import QTextEdit

from nebula import search_replace_dialog


def build_dialog(tmp_path, monkeypatch, text):
    stylesheet = tmp_path / "style.css"
    stylesheet.write_text("QDialog { color: white; }")
    monkeypatch.setattr(search_replace_dialog, "return_path", lambda _: str(stylesheet))

    text_edit = QTextEdit()
    text_edit.setPlainText(text)
    dialog = search_replace_dialog.SearchReplaceDialog(text_edit)
    return dialog, text_edit


def test_search_replace_dialog_workflow(qapp, tmp_path, monkeypatch):
    messages = []
    dialog, text_edit = build_dialog(tmp_path, monkeypatch, "hello world hello")

    monkeypatch.setattr(
        search_replace_dialog.QMessageBox,
        "information",
        lambda parent, title, message: messages.append((title, message)),
    )

    try:
        assert dialog.windowTitle() == "Search and Replace"
        assert dialog.minimumWidth() == 300
        assert dialog.maximumWidth() == 500

        dialog.search_field.setText("hello")
        dialog.search_button.click()
        assert text_edit.textCursor().selectedText() == "hello"

        dialog.next_button.click()
        dialog.previous_button.click()
        dialog.performSearch("hello", next=False, backwards=True)
        dialog.performSearch("", next=True, backwards=False)

        dialog.search_button.click()
        dialog.replace_field.setText("hi")
        dialog.replace_button.click()
        assert text_edit.toPlainText().startswith("hi")

        dialog.search_field.setText("hi")
        dialog.replace_field.setText("bye")
        dialog.replace_all_button.click()
        assert ("Replace All", "All 1 occurrences replaced.") in messages

        dialog.performSearch("missing", next=True, backwards=False)
        assert ("Search", "Text not found.") in messages
    finally:
        dialog.close()
        text_edit.close()
