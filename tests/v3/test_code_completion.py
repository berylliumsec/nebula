"""Editor completions must stay inside the virtual project and a bounded time."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import jedi
from fastapi.testclient import TestClient

from nebula.v3 import api, code_completion, language_server
from nebula.v3.api import create_app
from nebula.v3.code_completion import complete
from nebula.v3.domain import Engagement
from nebula.v3.language_server import _VIRTUAL_ROOT
from nebula.v3.storage import NebulaStore


def test_completion_analyzes_the_buffer_under_the_virtual_project(
    monkeypatch,
) -> None:
    captured: list[dict[str, Any]] = []

    class RecordingScript:
        def __init__(
            self,
            code: str | None = None,
            *,
            path: str | None = None,
            environment: Any = None,
            project: Any = None,
        ) -> None:
            captured.append({"path": path, "project": project})

        def complete(self, line: int, column: int) -> list[Any]:
            return []

    monkeypatch.setattr(code_completion.jedi, "Script", RecordingScript)
    source = "import os\nos."

    complete(source, "/etc/cron.d/host_task.py", len(source))
    complete(source, "src/app.py", len(source))

    host, relative = captured
    assert str(host["path"]).startswith(str(_VIRTUAL_ROOT))
    assert "/etc/" not in str(host["path"])
    assert relative["path"] == f"{_VIRTUAL_ROOT}/src/app.py"
    for item in captured:
        project = item["project"]
        assert isinstance(project, jedi.Project)
        assert project.path == Path(str(_VIRTUAL_ROOT))
        assert project.sys_path == []
        assert project.smart_sys_path is False
        assert project.load_unsafe_extensions is False


def test_code_completions_route_is_bounded_by_the_analysis_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    store = NebulaStore(tmp_path / "nebula.db")
    engagement = store.create(Engagement(name="Completions"))
    release = threading.Event()

    def slow_complete(source: str, path: str, offset: int) -> list[dict[str, Any]]:
        release.wait(timeout=3)
        return [{"label": "late", "type": "variable"}]

    monkeypatch.setattr(api, "complete_code", slow_complete)
    monkeypatch.setattr(language_server, "ANALYSIS_TIMEOUT_SECONDS", 0.2)
    client = TestClient(create_app(store, auth_token="test-token"))
    try:
        started = time.monotonic()
        response = client.post(
            "/api/v1/code/completions",
            headers={"Authorization": "Bearer test-token"},
            json={
                "engagement_id": engagement.id,
                "path": "slow.py",
                "source": "import os\nos.",
                "offset": 13,
            },
        )
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert elapsed < 2
