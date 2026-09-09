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
    if os.environ.get("NEBULA_TEST_BROWSER_RUNTIME"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.environ[
            "NEBULA_TEST_BROWSER_RUNTIME"
        ]
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
        # A pre-v2 record: acceptance must prove upgrade readability without
        # allowing new content-inventory records through the current API.
        from sqlalchemy import insert
        from nebula.v3.application_model.persistence import graphs
        from nebula.v3.application_model.service import empty

        legacy_project = store.create(
            Engagement(id="legacy-model-regression", name="Legacy model")
        )
        legacy_graph = empty(legacy_project.id)
        legacy_graph["revision"] = 1
        legacy_graph["types"] = [
            dict(
                name="custom.LegacyDatabase",
                label="Legacy database",
                category="Dependencies",
                description="Historical specialization",
                extends="Database",
                properties=[],
                identity_hints=["name"],
                evidence_examples=["Historical record"],
            )
        ]
        legacy_graph["objects"]["legacy-asset"] = {
            "id": "legacy-asset",
            "label": "Historical resource",
            "authentication_context": "anonymous",
            "revision": 0,
            "classification": {
                "value": "Asset",
                "status": "hypothesized",
                "evidence": [],
                "reason": "",
                "review": "unreviewed",
            },
            "properties": {},
        }
        from copy import deepcopy

        for identifier in ("legacy-view", "legacy-next"):
            legacy_graph["objects"][identifier] = {
                **deepcopy(legacy_graph["objects"]["legacy-asset"]),
                "id": identifier,
                "label": identifier,
                "classification": {
                    **legacy_graph["objects"]["legacy-asset"]["classification"],
                    "value": "Page",
                },
            }
        legacy_graph["relationships"]["legacy-link"] = {
            "id": "legacy-link",
            "type": "links_to",
            "source": "legacy-view",
            "target": "legacy-next",
            "claim": {
                **legacy_graph["objects"]["legacy-asset"]["classification"],
                "value": True,
            },
        }
        with store.database.engine.begin() as connection:
            connection.execute(
                insert(graphs).values(
                    project_id=legacy_project.id, revision=1, payload=legacy_graph
                )
            )
        if os.environ.get("NEBULA_TEST_LARGE_WORKSPACE") == "1":
            with (linked / "large-existing.bin").open("wb") as stream:
                stream.truncate(6 * 1024**3)
            entries = linked / "many-files"
            entries.mkdir()
            for index in range(50_001):
                (entries / f"entry-{index}").touch()
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

        @app.post("/fixture-site/unused-harness-session/{project_id}")
        def unused_harness_session(project_id: str):
            from nebula.v3.domain import HarnessSession

            store.get(Engagement, project_id)
            return store.create(
                HarnessSession(
                    engagement_id=project_id,
                    harness_profile_id="fixture-only",
                    model="fixture-only",
                    status="starting",
                )
            )

        app.router.routes.insert(0, app.router.routes.pop())

        @app.get("/fixture-site/protected")
        def protected_fixture():
            return HTMLResponse(
                "<html><head><title>Site A</title></head><body><h1>Site A</h1><p>Access denied. Data query unavailable.</p></body></html>",
                status_code=403,
            )

        app.router.routes.insert(0, app.router.routes.pop())

        @app.get("/fixture-site/manual-input")
        def manual_input_fixture():
            return HTMLResponse("""<!doctype html><html><head><title>Manual input fixture</title></head>
<body style="margin:0;background:white;color:black;font:20px sans-serif">
<button style="position:absolute;left:40px;top:50px;width:240px;height:70px"
onclick="this.textContent='Clicked '+(++window.clicks)">Click fixture</button>
<input aria-label="Fixture text" style="position:absolute;left:40px;top:160px;width:240px;height:50px"
oninput="document.querySelector('output').textContent=this.value">
<output style="position:absolute;left:40px;top:240px"></output>
<script>window.clicks=0</script></body></html>""")

        app.router.routes.insert(0, app.router.routes.pop())
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19420")),
        )
