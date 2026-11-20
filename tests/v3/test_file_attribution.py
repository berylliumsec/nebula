"""Overview mode's file attribution: which chat changed which workspace file."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from datetime import timedelta

import pytest
from fastapi import FastAPI

from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    HarnessTurn,
    HarnessTurnOrigin,
    WorkspaceProvenanceObservation,
    utc_now,
)
from nebula.v3.file_attribution import (
    DiffHunk,
    file_attribution_router,
    parse_unified_diff,
)
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import AsyncTestClient as TestClient

pytestmark = pytest.mark.anyio


def test_unified_diff_hunks_are_new_file_line_runs():
    diff = "\n".join(
        [
            "diff --git a/src/app.py b/src/app.py",
            "index 1111111..2222222 100644",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -1,6 +1,6 @@",
            " import os",
            "-import sys",
            "+import sys  # changed",
            "+import json",
            " ",
            " def main():",
            "-    pass",
            "     return 0",
            "@@ -20,2 +21,3 @@ def tail():",
            " a",
            "+b",
            " c",
            "diff --git a/new.txt b/new.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/new.txt",
            "@@ -0,0 +1,2 @@",
            "+one",
            "+two",
            "\\ No newline at end of file",
            "diff --git a/old.txt b/old.txt",
            "deleted file mode 100644",
            "--- a/old.txt",
            "+++ /dev/null",
            "@@ -1,1 +0,0 @@",
            "-gone",
        ]
    )

    app, created, deleted = parse_unified_diff(diff)

    assert (app.path, app.kind, app.added, app.removed) == (
        "src/app.py",
        "modified",
        3,
        2,
    )
    assert app.hunks == [
        DiffHunk(2, 2, "changed"),
        DiffHunk(6, 0, "changed"),
        DiffHunk(22, 1, "added"),
    ]
    assert (created.path, created.kind, created.added) == ("new.txt", "created", 2)
    assert created.hunks == [DiffHunk(1, 2, "added")]
    assert (deleted.path, deleted.kind, deleted.removed) == ("old.txt", "deleted", 1)
    assert deleted.hunks == [DiffHunk(1, 0, "changed")]


def test_headerless_patch_is_one_anonymous_file():
    (only,) = parse_unified_diff("@@ -3 +3,2 @@\n-x\n+y\n+z\n")
    assert only.path is None
    assert (only.added, only.removed) == (2, 1)
    assert only.hunks == [DiffHunk(3, 2, "changed")]


async def _fixture(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = await store.create(
        Engagement(id="p", name="Project", workspace_path=str(workspace))
    )
    await store.create(Engagement(id="other", name="Other"))

    async def chat(identity, title):
        return await store.create(
            ChatSession(
                id=identity,
                engagement_id="p",
                title=title,
                provider_profile_id="provider",
                model="m",
            )
        )

    async def turn(session, identity, status, **fields):
        return await store.create(
            ChatTurn(
                id=identity,
                engagement_id="p",
                session_id=session.id,
                provider_profile_id="provider",
                model="m",
                status=status,
                **fields,
            )
        )

    provider = await chat("provider-chat", "Fix the parser")
    codex = await chat("codex-chat", "Refactor app")
    claude = await chat("claude-chat", "Tidy docs")
    await turn(provider, "pt-1", ChatTurnStatus.COMPLETE)
    await turn(provider, "pt-2", ChatTurnStatus.ROUTING)
    await turn(codex, "ct-1", ChatTurnStatus.FAILED, error="boom")
    await turn(claude, "lt-1", ChatTurnStatus.COMPLETE)

    now = utc_now()
    await store.create(
        WorkspaceProvenanceObservation(
            engagement_id="p",
            workspace_root=str(workspace),
            scope_kind="turn",
            scope_id="pt-2",
            actor_id="chat:provider-chat",
            chat_session_id="provider-chat",
            chat_turn_id="pt-2",
            status="complete",
            started_at=now - timedelta(minutes=3),
            completed_at=now - timedelta(minutes=2),
            mutations=[
                {
                    "path": "src/parser.py",
                    "before": None,
                    "after": {"status": " M", "kind": "file", "sha256": "a" * 64},
                    "confidence": "exact",
                },
                {
                    "path": "notes/new.md",
                    "before": None,
                    "after": {"status": "??", "kind": "file", "sha256": "b" * 64},
                    "confidence": "uncertain_parallel_turns",
                },
                {
                    "path": "committed.txt",
                    "before": {"status": " M", "kind": "file"},
                    "after": None,
                    "confidence": "exact",
                },
            ],
        )
    )
    for identity, session_id, turn_id in [
        ("ht-codex", "codex-chat", "ct-1"),
        ("ht-claude", "claude-chat", "lt-1"),
    ]:
        await store.create(
            HarnessTurn(
                id=identity,
                engagement_id="p",
                harness_session_id=f"hs-{identity}",
                origin=HarnessTurnOrigin.CHAT,
                chat_session_id=session_id,
                chat_turn_id=turn_id,
                prompt="Do it",
            )
        )
    await store.append_operation_event(
        "ht-codex",
        "harness_turn",
        "p",
        "harness.tool_completed",
        {
            "type": "tool_completed",
            "item_id": "fc-1",
            "item_kind": "file_change",
            "item_status": "completed",
            "occurred_at": (now - timedelta(minutes=1)).isoformat(),
            "payload": {
                "type": "fileChange",
                "changes": [
                    {
                        "path": str(workspace / "src" / "parser.py"),
                        "kind": {"type": "update"},
                        "diff": "@@ -10,2 +10,3 @@\n a\n-b\n+B\n+C\n",
                    },
                    {"path": "/etc/passwd", "kind": {"type": "update"}, "diff": ""},
                ],
            },
        },
    )
    await store.append_operation_event(
        "ht-claude",
        "harness_turn",
        "p",
        "harness.tool_started",
        {
            "type": "tool_started",
            "item_id": "tu-1",
            "item_kind": "file_change",
            "item_status": "running",
            "tool_name": "Edit",
            "payload": {
                "id": "tu-1",
                "arguments": {
                    "file_path": str(workspace / "README.md"),
                    "old_string": "one\ntwo\n",
                    "new_string": "one\n2\nthree\n",
                },
            },
        },
    )
    await store.append_operation_event(
        "ht-claude",
        "harness_turn",
        "p",
        "harness.tool_completed",
        {
            "type": "tool_completed",
            "item_id": "tu-1",
            "item_kind": "file_change",
            "item_status": "completed",
            "payload": {"result": "ok"},
        },
    )
    app = FastAPI()
    app.include_router(file_attribution_router(store), prefix="/api/v1")
    return store, project, TestClient(app, raise_server_exceptions=False)


async def test_summary_names_each_paths_latest_chat_and_its_state(tmp_path):
    _store, _project, client = await _fixture(tmp_path)

    response = await client.get("/api/v1/projects/p/files/attribution")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truncated"] is False
    rows = {row["path"]: row for row in body["files"]}
    assert set(rows) == {"src/parser.py", "notes/new.md", "README.md"}
    # The Codex patch is newer than the provider turn's observation.
    assert rows["src/parser.py"]["sessionId"] == "codex-chat"
    assert rows["src/parser.py"]["sessionTitle"] == "Refactor app"
    assert rows["src/parser.py"]["turnId"] == "ct-1"
    assert rows["src/parser.py"]["state"] == "failed"
    assert rows["notes/new.md"]["state"] == "working"
    assert rows["README.md"]["state"] == "idle"
    changed = [row["changedAt"] for row in body["files"]]
    assert changed == sorted(changed, reverse=True)

    etag = response.headers["etag"]
    again = await client.get(
        "/api/v1/projects/p/files/attribution", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304

    assert (
        await client.get("/api/v1/projects/missing/files/attribution")
    ).status_code == 404
    assert (
        await client.get(
            "/api/v1/projects/p/files/attribution", params={"since_hours": 721}
        )
    ).status_code == 422


async def test_history_lists_hunks_from_patches_and_paths_from_observations(tmp_path):
    _store, _project, client = await _fixture(tmp_path)

    response = await client.get(
        "/api/v1/projects/p/files/attribution/history",
        params={"path": "./src/parser.py"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == "src/parser.py"
    codex, provider = body["events"]
    assert codex == {
        "sessionId": "codex-chat",
        "sessionTitle": "Refactor app",
        "turnId": "ct-1",
        "turnIndex": 1,
        "at": codex["at"],
        "kind": "modified",
        "added": 2,
        "removed": 1,
        "hunks": [{"start": 11, "lines": 2, "kind": "changed"}],
        "attribution": "owned",
    }
    assert provider["sessionId"] == "provider-chat"
    assert provider["turnIndex"] == 2
    assert (provider["added"], provider["hunks"]) == (None, None)

    created = (
        await client.get(
            "/api/v1/projects/p/files/attribution/history",
            params={"path": "notes/new.md"},
        )
    ).json()["events"]
    assert [(item["kind"], item["attribution"]) for item in created] == [
        ("created", "uncertain")
    ]
    readme = (
        await client.get(
            "/api/v1/projects/p/files/attribution/history",
            params={"path": "README.md"},
        )
    ).json()["events"]
    assert [(item["added"], item["removed"], item["hunks"]) for item in readme] == [
        (2, 1, None)
    ]

    for bad in ["../secret", "/etc/passwd", "a/../../b"]:
        rejected = await client.get(
            "/api/v1/projects/p/files/attribution/history", params={"path": bad}
        )
        assert rejected.status_code == 422, bad
    missing = await client.get(
        "/api/v1/projects/missing/files/attribution/history",
        params={"path": "a.txt"},
    )
    assert missing.status_code == 404
