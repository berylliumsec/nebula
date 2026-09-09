"""Disposable real-Core host for application-model browser acceptance."""

import os
import tempfile
from pathlib import Path
from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.workspace import WorkspaceService
from nebula.v3.domain import Engagement
import uvicorn
from starlette.responses import HTMLResponse


class FixtureWorkspacePaths:
    """Disposable filesystem adapter; all listing/upload/reset use real WorkspaceService."""

    def __init__(self, root, store):
        self.root = root
        self.store = store

    def workspace_for(self, project_id):
        project = self.store.get(Engagement, project_id)
        path = (
            Path(project.workspace_path)
            if project.workspace_path
            else self.root / project_id
        )
        path.mkdir(parents=True, exist_ok=True)
        return path


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nebula-model-acceptance-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        artifacts = ArtifactStore(root / "artifacts")
        linked = root / "linked"
        linked.mkdir()
        (linked / "linked-inspection.txt").write_text(
            "Linked folder inspection remains visible."
        )
        store.create(
            Engagement(
                id="linked-model-regression",
                name="Linked workspace",
                workspace_path=str(linked),
            )
        )
        app = create_app(
            store,
            artifact_store=artifacts,
            workspace_service=WorkspaceService(
                store=store,
                artifact_store=artifacts,
                tool_platform=FixtureWorkspacePaths(root / "workspaces", store),
            ),
            auth_token="model-test-token",
            allow_insecure_device_pairing=True,
            static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
        )

        @app.get("/fixture-site/protected")
        def protected_fixture():
            return HTMLResponse(
                "<html><head><title>Site A</title></head><body><h1>Site A</h1><p>Access denied. Data query unavailable.</p></body></html>",
                status_code=403,
            )

        app.router.routes.insert(0, app.router.routes.pop())
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19420")),
        )
