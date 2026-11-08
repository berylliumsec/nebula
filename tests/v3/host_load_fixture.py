"""Disposable production-UI Core with inert provider and controlled host pressure."""

import asyncio
import hashlib
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time

from fastapi import HTTPException, Request
from keyring.backends.null import Keyring
import uvicorn

from nebula.v3 import api as api_module, chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
)
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore


class ManualMonitor(HostLoadMonitor):
    def __init__(self):
        super().__init__(interval=0.05)
        self.value = HostLoadSnapshot(
            time.monotonic(), 4, 0.95, 0.5, True, "Waiting for host CPU capacity", True
        )

    def cached(self):
        return self.value

    def sample(self):
        return self.value

    def recover(self):
        self.value = replace(
            self.value,
            sampled_at=self.value.sampled_at + 1,
            cpu_ratio=0.1,
            busy=False,
            reason=None,
        )


class InertProvider(ModelProvider):
    calls = 0

    def __init__(self, identity):
        super().__init__(
            ProviderConfig(
                id=identity,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                base_url="http://127.0.0.1:1/v1",
                default_model="model-a",
                model_allowlist=["model-a"],
                local=True,
                capabilities=ModelCapabilities(streaming=True),
            )
        )

    async def complete(self, request):
        if request.metadata.get("operation") == "conversation_naming":
            text = "Synthetic capacity chat"
        else:
            self.calls += 1
            text = "Synthetic host capacity answer."
        return ModelResponse(
            provider_id=self.config.id,
            model="model-a",
            text=text,
            usage=ModelUsage(input_tokens=2, output_tokens=4, total_tokens=6),
            finish_reason="stop",
        )

    async def health(self):
        return ProviderHealth(
            provider_id=self.config.id, healthy=True, models=["model-a"]
        )


async def main():
    with tempfile.TemporaryDirectory(prefix="nebula-host-admission-") as directory:
        root = Path(directory)
        store = NebulaStore(root / "core.db")
        await store.create(
            ProviderProfile(
                id="capacity-provider",
                name="Synthetic provider",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["model-a"],
                capability_verifications={
                    "model-a": ProviderCapabilityVerification(
                        model="model-a",
                        status=ProviderVerificationStatus.FAILED,
                        failure_detail="Synthetic text-only provider has no tool calls",
                    )
                },
                metadata={"default_model": "model-a"},
            )
        )
        profiles = [
            "model-desktop",
            "model-compact",
            *[
                f"model-{engine}-{width}"
                for width in (320, 390, 430)
                for engine in ("chromium", "webkit")
            ],
        ]
        for profile in profiles:
            workspace = root / profile
            workspace.mkdir()
            project = await store.create(
                Engagement(
                    id="capacity-" + profile,
                    name="Synthetic host capacity",
                    workspace_path=str(workspace),
                )
            )
            await store.create(
                ChatSession(
                    id="chat-" + profile,
                    engagement_id=project.id,
                    title="Host capacity check",
                    provider_profile_id="capacity-provider",
                    model="model-a",
                    metadata={"include_knowledge": False, "planning_enabled": False},
                )
            )
        provider = InertProvider("capacity-provider")
        api_module.provider_from_profile = lambda _: provider
        chat_module.provider_from_profile = lambda _: provider
        monitor = ManualMonitor()
        bundle = Path(__file__).resolve().parents[2] / "ui" / "dist"
        digest = hashlib.sha256()
        for file in sorted(bundle.rglob("*")):
            if file.is_file():
                digest.update(
                    str(file.relative_to(bundle)).encode() + b"\0" + file.read_bytes()
                )
        app = create_app(
            store,
            artifact_store=ArtifactStore(root / "artifacts"),
            credential_store=CredentialStore(keyring_backend=Keyring()),
            auth_token="host-load-test-token",
            execution_data_root=root,
            host_load_gate=HostLoadGate(monitor),
            static_dir=bundle,
            allow_insecure_device_pairing=True,
        )

        # Fixture controls must precede Core's final catch-all static mount.
        workspace_mount = app.router.routes.pop()
        assert getattr(workspace_mount, "name", None) == "workspace"

        @app.post("/__host_fixture/load")
        async def set_pressure(request: Request):
            if request.headers.get("Authorization") != "Bearer host-load-test-token":
                raise HTTPException(401)
            body = await request.json()
            if body.get("busy") is True:
                fresh = ManualMonitor()
                monitor.value = fresh.value
            else:
                monitor.recover()
            if body.get("io"):
                monitor.value = replace(
                    monitor.value,
                    cpu_ratio=0.1,
                    busy=True,
                    reason="Waiting for host I/O capacity",
                    io_pressure_ratio=0.6,
                    load_average_ratio=2.0,
                )
            if body.get("reset_calls"):
                provider.calls = 0
            return {
                "busy": monitor.value.busy,
                "calls": provider.calls,
                "bundle_sha256": digest.hexdigest(),
            }

        app.router.routes.append(workspace_mount)

        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=int(os.environ.get("NEBULA_MODEL_TEST_PORT", "19480")),
                access_log=False,
            )
        ).serve()


if __name__ == "__main__":
    asyncio.run(main())
