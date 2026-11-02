"""Synthetic runtime metadata; all container, SSH and harness I/O is inert."""

import json
from pathlib import Path
from typing import Any

import httpx

from nebula.v3 import cli
from nebula.v3 import api as core_api
from nebula.v3.domain import Engagement, RunnerProfile
from nebula.v3.environments import SshEnvironmentService, SshEnvironmentSettings
from nebula.v3.harnesses import HarnessCapabilities, HarnessHealth
from nebula.v3.ssh_environments import SshProbeResult
from nebula.v3.web_search import EXPECTED_REPOSITORY, SearchRuntime, WebSearchResult

_create_app = cli.create_app


def public_directory_transport():
    def respond(request):
        if (
            str(request.url) != "https://openrouter.ai/api/v1/providers"
            or "authorization" in request.headers
        ):
            raise RuntimeError("Unexpected public directory request")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "slug": "fixture-vendor",
                        "name": "Fixture vendor",
                        "headquarters": "US",
                        "datacenters": ["US"],
                    }
                ]
            },
        )

    return httpx.MockTransport(respond)


async def metadata_local_services(_factory):
    return [
        core_api.LocalProviderDetection(
            flavor="vllm",
            display_name="Fixture localhost service",
            endpoint="http://127.0.0.1:8000/v1",
            models=["fixture-model"],
        )
    ]


class MetadataSshService(SshEnvironmentService):
    def discover(self, *, resolve=True):
        return super().discover(resolve=False)


class InertSearchRuntime(SearchRuntime):
    """Keep real lifecycle/storage logic; replace OS and HTTP boundaries only."""

    def __init__(self, *, store, data_root):
        super().__init__(store=store, data_root=data_root)
        self.phase = "absent"
        self.exit_codes = {}
        self.calls = []
        self.runner = RunnerProfile(
            id="inert-search",
            name="Inert search runner",
            runtime="podman",
            executable="/fixture/podman",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )

    def _profile(self):
        return self.runner

    async def _run(self, profile, *arguments, **kwargs):
        self.calls.append(arguments)
        command = arguments[0]
        if self.exit_codes.get(command):
            return "", "Synthetic runtime refusal", self.exit_codes[command]
        if arguments[:2] == ("container", "inspect"):
            return (
                ("true" if self.phase == "ready" else "false", "", 0)
                if self.phase != "absent"
                else ("", "absent", 1)
            )
        if arguments[:2] == ("image", "inspect"):
            return (
                json.dumps(
                    {"RepoDigests": [EXPECTED_REPOSITORY + "@sha256:" + "a" * 64]}
                ),
                "",
                0,
            )
        if command == "pull":
            return "", "", 0
        if command in {"run", "stop", "rm"}:
            self.phase = {"run": "ready", "stop": "stopped", "rm": "absent"}[command]
            return "fixture-container", "", 0
        raise RuntimeError("Unexpected inert runtime command")

    async def _probe(self, port):
        return self.phase == "ready"

    async def _wait_ready(self, port):
        return await self._probe(port)

    def _schedule_idle_stop(self):
        pass

    async def search(self, query, *, count=5, freshness="any"):
        await self.ensure_ready()
        return [
            WebSearchResult(
                rank=1,
                title="Synthetic fixed sample",
                url="https://example.invalid",
                engine="wikipedia",
            )
        ]


class MetadataHarnessAdapter:
    async def probe(self, profile, credentials):
        return HarnessHealth(
            profile_id=profile.id,
            kind=profile.kind,
            healthy=True,
            authentication_state="verified",
            harness_version="fixture-1",
            capabilities=HarnessCapabilities(models=["fixture-model"]),
            detail="Synthetic harness health; no process or endpoint was opened",
        )


def create_fixture_app(*args: Any, **kwargs: Any):
    store = args[0] if args else kwargs["store"]
    platform = kwargs["tool_platform"]
    project = store.create(
        Engagement(name="Runtime administration fixture", status="active")
    )
    workspace = platform.workspace_for(project.id)
    skill = workspace / ".agents" / "skills" / "fixture-selected-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-selected-skill\ndescription: Synthetic metadata\n---\nInert fixture.\n"
    )
    (workspace / "AGENTS.md").write_text(
        "Synthetic administration fixture instructions.\n"
    )
    config = Path(platform.workspace_root).parent / "fixture-ssh-config"
    config.write_text(
        "Host fixture-ask\n    HostName example.invalid\nHost fixture-new\n    HostName example.invalid\n"
    )
    service = MetadataSshService(
        store,
        config_path=config,
        probe=lambda alias: SshProbeResult(
            alias=alias, status="reachable", latency_ms=1, system="Fixture OS"
        ),
    )
    service.save(
        "fixture-ask", SshEnvironmentSettings(display_name="SSH metadata fixture")
    )
    kwargs["ssh_environment_service"] = service
    store.create(
        RunnerProfile(
            id="fixture-runner",
            name="Runner metadata fixture",
            runtime="podman",
            executable="/fixture/podman",
            context="fixture",
            socket="/fixture/podman.sock",
            platform="linux/amd64",
            isolation="rootless",
            enabled=False,
            seccomp_profile="/fixture/seccomp.json",
        )
    )
    verify = platform.verify_runner

    async def metadata_only(identity):
        if identity == "fixture-runner":
            return store.get(RunnerProfile, identity)
        return await verify(identity)

    platform.verify_runner = metadata_only
    platform.search_runtime = InertSearchRuntime(store=store, data_root=config.parent)
    kwargs["openrouter_directory_transport"] = public_directory_transport()
    core_api._discover_local_provider_services = metadata_local_services
    app = _create_app(*args, **kwargs)
    app.state.harness_runtime_service.adapter_factory = lambda kind: (
        MetadataHarnessAdapter()
    )
    return app


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
