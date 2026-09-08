"""Disposable real-Core host for application-model browser acceptance."""

import os
import tempfile
from pathlib import Path
from nebula.v3.api import create_app
from nebula.v3.storage import NebulaStore
from nebula.v3.artifacts import ArtifactStore
import uvicorn


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nebula-model-acceptance-") as directory:
        root = Path(directory)
        app = create_app(
            NebulaStore(root / "core.db"),
            artifact_store=ArtifactStore(root / "artifacts"),
            auth_token="model-test-token",
            allow_insecure_device_pairing=True,
            static_dir=Path(__file__).resolve().parents[2] / "ui" / "dist",
        )
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19420")),
        )
