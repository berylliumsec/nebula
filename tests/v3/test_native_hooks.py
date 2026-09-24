import asyncio
import json
from pathlib import Path

import pytest

from nebula.v3.native_hooks import (
    NativeHookError,
    NativeHookRunner,
    discover_native_hooks,
    snapshot_native_hook,
)
from nebula.v3.storage import NebulaStore


def _hook(
    workspace: Path,
    *,
    name: str = "audit",
    script: str = "#!/bin/sh\npython3 -c 'import json,sys; print(json.load(sys.stdin)[\"event\"])'\n",
    timeout_seconds: int = 5,
) -> Path:
    directory = workspace / ".agents" / "hooks" / name
    directory.mkdir(parents=True)
    executable = directory / "run.sh"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)
    (directory / "hook.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "Audit lifecycle",
                "description": "Records native chat lifecycle events.",
                "events": ["chat.turn.started", "chat.turn.completed"],
                "command": ["run.sh"],
                "timeout_seconds": timeout_seconds,
                "side_effects": "workspace",
                "failure_policy": "continue",
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_native_hook_catalog_uses_agents_root_and_freezes_source_digests(tmp_path):
    workspace = tmp_path / "workspace"
    directory = _hook(workspace)
    _hook(workspace / ".codex", name="ignored")
    _hook(workspace / ".grok", name="ignored")
    catalog = discover_native_hooks(workspace)

    assert [item.id for item in catalog] == ["audit"]
    assert catalog[0].path == str(directory.resolve())
    snapshot = snapshot_native_hook("audit", catalog)
    assert snapshot.manifest.events == ["chat.turn.started", "chat.turn.completed"]
    assert snapshot.manifest_sha256 == catalog[0].manifest_sha256
    assert snapshot.executable_sha256 == catalog[0].executable_sha256

    (directory / "run.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(NativeHookError, match="executable changed"):
        asyncio.run(
            NativeHookRunner(NebulaStore(tmp_path / "tamper.db")).run(
                snapshot,
                engagement_id="eng",
                chat_session_id="session",
                chat_turn_id="turn",
                event_name="chat.turn.started",
                payload={},
            )
        )


def test_native_hook_runner_persists_versioned_bounded_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(workspace)
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "hooks.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="session",
            chat_turn_id="turn",
            event_name="chat.turn.started",
            payload={"model": "author/model"},
        )
    )

    assert execution.status == "complete"
    assert execution.event_version == 1
    assert execution.stdout.strip() == "chat.turn.started"
    assert execution.side_effects == "workspace"
    assert store.get(type(execution), execution.id) == execution


def test_native_hook_envelope_exposes_generic_actor_and_workspace_provenance(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(
        workspace,
        script=(
            "#!/bin/sh\n"
            "python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True))'\n"
        ),
    )
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "envelope.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="child-session",
            chat_turn_id="child-turn",
            owner_kind="chat",
            owner_id="child-session",
            event_name="chat.turn.completed",
            payload={"model": "model"},
            workspace_provenance={
                "schema": "nebula.workspace-provenance/v1",
                "actor_id": "subagent:child-agent",
                "supported": True,
                "confidence": "exact",
                "attribution": {"owned": [{"path": "owned.txt"}]},
            },
        )
    )
    envelope = json.loads(execution.stdout)

    assert envelope["actor"] == {
        "id": "subagent:child-agent",
        "owner_kind": "chat",
        "owner_id": "child-session",
        "chat_session_id": "child-session",
        "chat_turn_id": "child-turn",
    }
    assert envelope["workspace_provenance"]["schema"] == (
        "nebula.workspace-provenance/v1"
    )
    assert envelope["workspace_provenance"]["attribution"]["owned"] == [
        {"path": "owned.txt"}
    ]


def test_native_hook_runner_times_out_without_treating_output_as_approval(tmp_path):
    workspace = tmp_path / "workspace"
    _hook(
        workspace,
        script="#!/bin/sh\nprintf '{\"approval\":true}'\nsleep 2\n",
        timeout_seconds=1,
    )
    snapshot = snapshot_native_hook("audit", discover_native_hooks(workspace))
    store = NebulaStore(tmp_path / "timeout.db")

    execution = asyncio.run(
        NativeHookRunner(store).run(
            snapshot,
            engagement_id="eng",
            chat_session_id="session",
            chat_turn_id="turn",
            event_name="chat.turn.started",
            payload={"approval_id": "approval"},
        )
    )

    assert execution.status == "timed_out"
    assert execution.error == "hook timed out"
    assert execution.stdout == ""
    assert execution.reconciliation is None


def test_native_hook_catalog_accepts_project_tool_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    directory = _hook(workspace)
    manifest = json.loads((directory / "hook.json").read_text(encoding="utf-8"))
    manifest["events"] = ["tool.before", "tool.after"]
    (directory / "hook.json").write_text(json.dumps(manifest), encoding="utf-8")

    [descriptor] = discover_native_hooks(workspace)

    assert descriptor.manifest.events == ["tool.before", "tool.after"]
