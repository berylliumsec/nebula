import os
import sys
import tempfile
import gc
from pathlib import Path

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="nebula-tests-")

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _drain_qt(app):
    QCoreApplication.sendPostedEvents(None, 0)
    app.processEvents()


def _cleanup_widgets(app):
    _drain_qt(app)
    for widget in list(app.topLevelWidgets()):
        try:
            widget.close()
        except Exception:
            pass

    _drain_qt(app)
    for widget in list(app.topLevelWidgets()):
        try:
            widget.deleteLater()
        except Exception:
            pass

    _drain_qt(app)
    gc.collect()


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        from nebula.initial_logic import MainApplication

        app = MainApplication([])
    yield app
    _cleanup_widgets(app)
    app.quit()
    _drain_qt(app)


@pytest.fixture(autouse=True)
def cleanup_qt_widgets(qapp):
    yield
    _cleanup_widgets(qapp)
