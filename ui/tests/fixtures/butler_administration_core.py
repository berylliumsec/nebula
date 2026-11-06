"""Synthetic Core metadata and a disposable Git tree; no model program execution."""

import subprocess
from typing import Any

from nebula.v3 import cli
from nebula.v3.domain import ChatSession, Engagement, Observation

if __package__:
    from .butler_runtime_settings_core import create_fixture_app as _create
else:
    from butler_runtime_settings_core import create_fixture_app as _create


async def create_fixture_app(*args: Any, **kwargs: Any):
    app = _create(*args, **kwargs)
    store = app.state.store
    project = next(
        row
        for row in (await store.list_entities(Engagement, limit=100))
        if row.name == "Runtime administration fixture"
    )
    workspace = kwargs["tool_platform"].workspace_for(project.id)
    (workspace / "fixture.txt").write_text("Saved fixture\n")
    for command in (
        ["git", "init", "--quiet", str(workspace)],
        ["git", "-C", str(workspace), "add", "fixture.txt"],
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "Synthetic fixture",
        ],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=15)
    (workspace / "fixture.txt").write_text("Changed fixture\n")
    (
        await store.create(
            Observation(
                observation_type="note",
                engagement_id=project.id,
                title="Selected metadata note",
                body="Synthetic saved note.",
            )
        )
    )
    (
        await store.create(
            ChatSession(
                provider_profile_id="fixture-provider",
                engagement_id=project.id,
                title="Selected metadata conversation",
                model="fixture",
            )
        )
    )
    (
        await store.create(
            ChatSession(
                provider_profile_id="fixture-provider",
                engagement_id=project.id,
                title="Selected metadata private Butler",
                model="fixture",
                metadata={"control_assistant": True},
            )
        )
    )
    return app


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
