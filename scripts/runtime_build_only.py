"""Build the canonical workstation image without admission or runtime execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from nebula.v3.sandbox import (
    ContainerImagePreparer,
    ContainerRuntimeType,
    ContainerSandboxRunner,
    RunnerIsolationMode,
    RunnerPlatform,
    RunnerProfile,
)


async def build_only(source_reference: str) -> dict[str, object]:
    if not re.fullmatch(
        r"docker.io/kalilinux/kali-rolling@sha256:[0-9a-f]{64}", source_reference
    ):
        raise ValueError("the official Kali source must be pinned by sha256 digest")
    runner = ContainerSandboxRunner(
        profile=RunnerProfile(
            runtime_type=ContainerRuntimeType.PODMAN,
            executable=Path("/usr/bin/podman"),
            platform=RunnerPlatform.LINUX,
            isolation_mode=RunnerIsolationMode.LINUX_ROOTLESS,
        )
    )
    preparer = ContainerImagePreparer(
        runner=runner,
        platform="linux/amd64",
        source_reference=source_reference,
        expected_repository="docker.io/kalilinux/kali-rolling",
    )
    stdout, stderr, code = await preparer._runtime_command(
        "info", "--format", "{{.Host.Security.Rootless}}", timeout_seconds=30
    )
    if code or stdout.strip() != "true":
        raise RuntimeError(f"rootless Podman is required: {stderr}")
    tag = "localhost/nebula-runtime-build-only:verification"
    # prepare()/admission would start runtime checks. Invoke only the production
    # recipe's build phase; package inventory reads metadata, not security tools.
    stdout, stderr, code = await preparer._build_derived_image(source_reference, tag)
    if code:
        raise RuntimeError(f"canonical image build failed:\n{stdout}\n{stderr}")
    stdout, stderr, code = await preparer._runtime_command(
        "image", "inspect", tag, timeout_seconds=30
    )
    if code:
        raise RuntimeError(f"built image metadata could not be read: {stderr}")
    return {
        "source_reference": source_reference,
        "platform": preparer.platform,
        "recipe_version": preparer._recipe_version,
        "runtime_execution": False,
        "image_metadata": json.loads(stdout),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = asyncio.run(build_only(args.source_reference))
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
