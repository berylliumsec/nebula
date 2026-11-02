"""Synthetic runner/SSH metadata only; never execute either host program."""

from pathlib import Path
from typing import Any

from nebula.v3 import cli
from nebula.v3.domain import RunnerProfile
from nebula.v3.environments import SshEnvironmentService, SshEnvironmentSettings

_create_app = cli.create_app


class MetadataSshService(SshEnvironmentService):
    def discover(self, *, resolve=True):
        return super().discover(resolve=False)

    async def probe(self, alias):
        raise RuntimeError("Runtime Settings fixture must not execute SSH")


def create_fixture_app(*args: Any, **kwargs: Any):
    store = args[0] if args else kwargs["store"]
    platform = kwargs["tool_platform"]
    config = Path(platform.workspace_root).parent / "fixture-ssh-config"
    config.write_text(
        "Host fixture-ask\n    HostName example.invalid\nHost fixture-new\n    HostName example.invalid\n"
    )
    service = MetadataSshService(store, config_path=config)
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
    return _create_app(*args, **kwargs)


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
