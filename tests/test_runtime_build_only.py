import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from scripts import runtime_build_only


SOURCE = "docker.io/kalilinux/kali-rolling@sha256:" + "a" * 64


def test_build_only_uses_canonical_recipe_and_metadata_without_runtime(monkeypatch):
    commands = []

    async def command(self, *arguments, timeout_seconds):
        commands.append(arguments)
        if arguments[0] == "info":
            return "true\n", "", 0
        assert arguments[:2] == ("image", "inspect")
        return json.dumps([{"Id": "built-image"}]), "", 0

    recipe = AsyncMock(return_value=("built-image", "", 0))
    admission = AsyncMock(side_effect=AssertionError("runtime admission forbidden"))
    execution = AsyncMock(side_effect=AssertionError("runtime execution forbidden"))
    monkeypatch.setattr(
        runtime_build_only.ContainerImagePreparer, "_runtime_command", command
    )
    monkeypatch.setattr(
        runtime_build_only.ContainerImagePreparer, "_build_derived_image", recipe
    )
    monkeypatch.setattr(runtime_build_only.ContainerImagePreparer, "prepare", admission)
    monkeypatch.setattr(runtime_build_only.ContainerSandboxRunner, "run", execution)
    receipt = asyncio.run(runtime_build_only.build_only(SOURCE))
    recipe.assert_awaited_once_with(
        SOURCE, "localhost/nebula-runtime-build-only:verification"
    )
    assert [args[0] for args in commands] == ["info", "image"]
    assert receipt["source_reference"] == SOURCE
    assert receipt["runtime_execution"] is False
    assert receipt["image_metadata"] == [{"Id": "built-image"}]
    admission.assert_not_awaited()
    execution.assert_not_awaited()


@pytest.mark.parametrize(
    "source",
    [
        "docker.io/kalilinux/kali-rolling:latest",
        "example.com/untrusted@sha256:" + "a" * 64,
    ],
)
def test_build_only_rejects_unpinned_or_unofficial_source(source):
    with pytest.raises(ValueError, match="official Kali source"):
        asyncio.run(runtime_build_only.build_only(source))


def test_build_only_stops_before_build_for_nonrootless_runtime(monkeypatch):
    recipe = AsyncMock(side_effect=AssertionError("build forbidden"))
    monkeypatch.setattr(
        runtime_build_only.ContainerImagePreparer,
        "_runtime_command",
        AsyncMock(return_value=("false", "", 0)),
    )
    monkeypatch.setattr(
        runtime_build_only.ContainerImagePreparer, "_build_derived_image", recipe
    )
    with pytest.raises(RuntimeError, match="rootless Podman is required"):
        asyncio.run(runtime_build_only.build_only(SOURCE))
    recipe.assert_not_awaited()
