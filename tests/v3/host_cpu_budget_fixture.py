"""Owned real Core/production UI; quota I/O is an inert observable test boundary."""

import hashlib
import os
from pathlib import Path
import tempfile

from fastapi import HTTPException, Request
from keyring.backends.null import Keyring
import uvicorn

from nebula.v3.api import create_app
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import Engagement
from nebula.v3.host_cpu_budget import HostCpuBudget, HostCpuUpdate
from nebula.v3.storage import NebulaStore


class Executor:
    limit = 2.0
    fail = False

    async def capacity(self):
        return 4.0

    async def apply(self, value):
        if self.fail:
            raise RuntimeError("Synthetic executor application failure")
        self.limit = value

    async def observe(self):
        return self.limit


def main():
    with tempfile.TemporaryDirectory(prefix="nebula-host-cpu-ui-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        store.create(Engagement(id="cpu-project", name="Synthetic host CPU settings"))
        executor = Executor()
        service = HostCpuBudget(
            root / "cpu.json",
            executor.capacity,
            apply=executor.apply,
            observe=executor.observe,
        )
        bundle = Path(__file__).resolve().parents[2] / "ui/dist"
        digest = hashlib.sha256()
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                digest.update(
                    str(path.relative_to(bundle)).encode() + b"\0" + path.read_bytes()
                )
        app = create_app(
            store,
            host_cpu_budget=service,
            auth_token="cpu-budget-test-token",
            execution_data_root=root,
            static_dir=bundle,
            credential_store=CredentialStore(keyring_backend=Keyring()),
            allow_insecure_device_pairing=True,
        )
        mount = app.router.routes.pop()
        assert getattr(mount, "name", None) == "workspace"

        @app.post("/__cpu_fixture/state")
        async def state(request: Request):
            if request.headers.get("Authorization") != "Bearer cpu-budget-test-token":
                raise HTTPException(401)
            body = await request.json()
            executor.fail = bool(body.get("fail"))
            if body.get("reset"):
                service.apply, service.observe = executor.apply, executor.observe
                executor.fail = False
                current = await service.status()
                await service.update(
                    HostCpuUpdate(
                        host_id=current.host_id,
                        expected_revision=current.revision,
                        cpu_limit=None,
                    )
                )
            if body.get("inactive"):
                service.apply = service.observe = None
            return {
                "bundle_sha256": digest.hexdigest(),
                "executor_limit": executor.limit,
            }

        app.router.routes.append(mount)
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19484")),
            access_log=False,
            log_level="warning",
        )
        store.database.dispose()


if __name__ == "__main__":
    main()
