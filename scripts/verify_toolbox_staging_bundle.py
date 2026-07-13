"""Install a staging Toolbox bundle through Nebula's production verifier."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Literal, Sequence, cast

from nebula.v3.database import Database
from nebula.v3.domain import ToolPackInstallationStatus
from nebula.v3.sandbox import (
    ContainerRuntimeType,
    ContainerSandboxRunner,
    ContainerToolPackRuntimeAdapter,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.toolpack_sdk import read_tool_pack
from nebula.v3.toolpacks import (
    Ed25519Keyring,
    ImmutableManifestStore,
    ToolPackInstaller,
)


class StagingCIRunner(ContainerSandboxRunner):
    """Use the hosted runner after the workflow has established its trust boundary."""

    async def available(self) -> tuple[bool, str]:
        return True, "staging CI container runner is available"


async def verify_bundle(
    *, bundle: Path, platform: str, runtime: Path
) -> dict[str, object]:
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("platform must be linux/amd64 or linux/arm64")
    selected_platform = cast(Literal["linux/amd64", "linux/arm64"], platform)
    executable = runtime.expanduser().resolve(strict=True)
    if executable.name != "docker":
        raise ValueError("staging bundle verification requires Docker")

    archive = read_tool_pack(bundle.expanduser().resolve(strict=True))
    with tempfile.TemporaryDirectory(
        prefix="nebula-toolbox-install-check-"
    ) as temporary:
        root = Path(temporary)
        runner = StagingCIRunner(
            profile=RunnerProfile(
                runtime_type=ContainerRuntimeType.DOCKER,
                executable=executable,
                platform=RunnerPlatform.LINUX,
                isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
            ),
            workspace_roots=[root],
        )
        installer = ToolPackInstaller(
            store=NebulaStore(Database(root / "nebula.db")),
            manifests=ImmutableManifestStore(root / "tool-packs"),
            runtime=ContainerToolPackRuntimeAdapter(
                runner=runner,
                platform=selected_platform,
            ),
            runtime_profile_id="staging-ci",
            platform=selected_platform,
            verifier=Ed25519Keyring({}),
            developer_mode=True,
        )
        installation = await installer.install(
            archive.manifest,
            source="staging-ci-local-bundle",
            signature=None,
            local_file=True,
            confirm_unsigned_permissions=True,
        )
    if installation.status != ToolPackInstallationStatus.READY:
        raise RuntimeError(
            f"staging bundle installation ended in {installation.status.value}"
        )
    return {
        "identity": archive.manifest.identity,
        "platform": selected_platform,
        "status": installation.status.value,
        "verified_at": installation.verified_at,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--runtime", type=Path, default=Path("/usr/bin/docker"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = asyncio.run(
        verify_bundle(
            bundle=args.bundle,
            platform=args.platform,
            runtime=args.runtime,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
