import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import nebula.v3.api as api_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    Engagement,
    EngagementToolAssignment,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    ToolPackInstallationStatus,
    ToolPackTrust,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_platform import (
    ToolPackEventJournal,
    ToolPlatform,
    ToolPlatformError,
)
from nebula.v3.toolpack_sdk import ToolPackSDKError
from nebula.v3.toolpacks import (
    RuntimeImageInfo,
    RuntimeSmokeResult,
    compile_manifest_yaml,
)


TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DIGEST = "a" * 64


def _auth_protocol() -> str:
    encoded = base64.urlsafe_b64encode(TOKEN.encode()).decode().rstrip("=")
    return f"nebula.auth.{encoded}"


def _platform(tmp_path, *, retention=256) -> ToolPlatform:
    return ToolPlatform(
        store=NebulaStore(tmp_path / "nebula.db"),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "tool-packs",
        developer_mode=True,
        event_retention=retention,
    )


def _manifest():
    return compile_manifest_yaml(
        f"""api_version: tools.nebula.security/v1
kind: ToolPack
metadata:
  publisher: example
  name: safe-local
  version: 1.0.0
  minimum_nebula_version: 3.0.0a1
  description: Safe local test pack.
  licenses: [BSD-3-Clause]
images:
  - name: sample
    platform: linux/amd64
    image: example.invalid/sample@sha256:{DIGEST}
    sbom: sample.cdx.json
    provenance: sample.intoto.jsonl
permissions: {{network: false, workspace: none, credentials: []}}
tools:
  - name: sample.query
    description: Query a local fixture.
    image: sample
    executable: /usr/bin/sample
    input_schema:
      type: object
      properties: {{query: {{type: string}}}}
      required: [query]
      additionalProperties: false
    output_schema:
      type: object
      properties: {{result: {{type: string}}}}
      required: [result]
      additionalProperties: false
    policy: {{risk_class: local_read}}
    parser: {{built_in: json/v1}}
    smoke_tests:
      - arguments: {{query: smoke}}
"""
    )


def test_journal_is_bounded_monotonic_and_reports_replay_truncation():
    journal = ToolPackEventJournal(retention=3)
    for number, phase in enumerate(
        ("pending", "pulling", "verifying", "ready", "failed"), start=1
    ):
        event = journal.append(
            operation_id=f"operation-{number}",
            operation="install_local",
            phase=phase,
        )
        assert event.sequence == number

    replay = journal.replay(after_sequence=0)
    assert [event.sequence for event in replay.events] == [3, 4, 5]
    assert replay.oldest_sequence == 3
    assert replay.latest_sequence == 5
    assert replay.truncated is True
    assert [event.sequence for event in journal.replay(4).events] == [5]
    assert journal.replay(4).truncated is False


def test_local_install_events_are_sanitized_and_ordered(tmp_path, monkeypatch):
    platform = _platform(tmp_path)
    manifest = _manifest()
    engagement = platform.store.create(Engagement(name="Existing engagement"))
    platform.store.create(
        RunnerProfile(
            id="PRIVATE-RUNNER-ID",
            name="Test runner",
            runtime=RunnerRuntime.PODMAN,
            executable="/usr/bin/podman",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
        )
    )
    monkeypatch.setattr(
        "nebula.v3.tool_platform.read_tool_pack",
        lambda _path: SimpleNamespace(manifest=manifest),
    )

    class FakeRuntime:
        async def pull(self, _image):
            return None

        async def inspect(self, image):
            return RuntimeImageInfo(
                image=image,
                digest=image.rsplit("@", 1)[1],
                platform="linux/amd64",
                user="10001:10001",
            )

        async def smoke_test(self, **_kwargs):
            return RuntimeSmokeResult(exit_code=0, stdout='{"result":"ok"}')

    monkeypatch.setattr(platform, "_runner", lambda _profile: object())
    monkeypatch.setattr(
        "nebula.v3.tool_platform.ContainerToolPackRuntimeAdapter",
        lambda **_kwargs: FakeRuntime(),
    )
    installed = asyncio.run(
        platform.install_local(
            b"PRIVATE-BUNDLE-CONTENT",
            runtime_profile_id="PRIVATE-RUNNER-ID",
            confirm_permissions=True,
        )
    )

    assert installed.trust == ToolPackTrust.LOCAL_TRUSTED
    [assignment] = platform.store.list_entities(
        EngagementToolAssignment,
        engagement_id=engagement.id,
    )
    assert assignment.enabled is True
    assert assignment.allowed_tool_names == ["sample.query"]

    # An explicit opt-out survives subsequent default-enablement passes.
    assignment = platform.store.update(
        EngagementToolAssignment,
        assignment.id,
        {"enabled": False},
        expected_revision=assignment.revision,
    )
    assert platform.enable_default_local_packs(engagement.id) == []
    assert (
        platform.store.get(EngagementToolAssignment, assignment.id).enabled is False
    )

    future = platform.store.create(Engagement(name="Future engagement"))
    [future_assignment] = platform.enable_default_local_packs(future.id)
    assert future_assignment.enabled is True
    assert future_assignment.manifest_digest == installed.manifest_digest

    events = platform.events.replay().events
    assert [event.phase for event in events] == [
        "pending",
        "pulling",
        "verifying",
        "ready",
    ]
    assert len({event.operation_id for event in events}) == 1
    assert events[-1].installation_id == installed.id
    assert events[-1].pack_identity == "example/safe-local@1.0.0"
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events], sort_keys=True
    )
    assert "PRIVATE-BUNDLE-CONTENT" not in serialized
    assert "PRIVATE-RUNNER-ID" not in serialized
    assert set(events[-1].model_dump()) == {
        "sequence",
        "occurred_at",
        "operation_id",
        "operation",
        "phase",
        "installation_id",
        "pack_identity",
        "manifest_digest",
        "result_status",
    }


def test_failed_event_does_not_replay_exception_or_bundle_content(
    tmp_path, monkeypatch
):
    platform = _platform(tmp_path)

    def fail_read(_path):
        raise ToolPackSDKError("PRIVATE-BUNDLE-CONTENT")

    monkeypatch.setattr("nebula.v3.tool_platform.read_tool_pack", fail_read)
    with pytest.raises(ToolPlatformError, match="PRIVATE-BUNDLE-CONTENT"):
        asyncio.run(
            platform.install_local(
                b"PRIVATE-BUNDLE-CONTENT",
                runtime_profile_id="PRIVATE-RUNNER-ID",
                confirm_permissions=True,
            )
        )

    events = platform.events.replay().events
    assert [event.phase for event in events] == ["pending", "failed"]
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events], sort_keys=True
    )
    assert "PRIVATE" not in serialized
    assert "ToolPackSDKError" not in serialized


def test_authenticated_tool_pack_websocket_replays_and_streams(tmp_path):
    platform = _platform(tmp_path, retention=2)
    for phase in ("pending", "pulling", "verifying"):
        platform.events.append(
            operation_id="operation-1",
            operation="install_catalog",
            phase=phase,
            manifest_digest=DIGEST,
        )
    app = create_app(
        platform.store,
        artifact_store=platform.artifact_store,
        auth_token=TOKEN,
        tool_platform=platform,
    )

    with TestClient(app) as client:
        with client.websocket_connect(
            "/api/v1/tool-packs/events/ws?after_sequence=0",
            subprotocols=["nebula.tool-packs.v1", _auth_protocol()],
        ) as websocket:
            assert websocket.accepted_subprotocol == "nebula.tool-packs.v1"
            assert websocket.receive_json()["event"]["sequence"] == 2
            assert websocket.receive_json()["event"]["sequence"] == 3
            complete = websocket.receive_json()
            assert complete == {
                "kind": "replay_complete",
                "after_sequence": 3,
                "oldest_sequence": 2,
                "latest_sequence": 3,
                "truncated": True,
            }
            for phase in ("pending", "pulling"):
                platform.events.append(
                    operation_id="operation-2",
                    operation="install_catalog",
                    phase=phase,
                    manifest_digest=DIGEST,
                )
            emitted = platform.events.append(
                operation_id="operation-2",
                operation="install_catalog",
                phase="ready",
                manifest_digest=DIGEST,
                result_status=ToolPackInstallationStatus.READY,
            )
            assert websocket.receive_json() == {
                "kind": "replay_gap",
                "after_sequence": 3,
                "oldest_sequence": 5,
                "latest_sequence": 6,
            }
            assert websocket.receive_json()["event"]["sequence"] == 5
            live = websocket.receive_json()
            assert live["kind"] == "event"
            assert live["event"]["sequence"] == emitted.sequence
            assert live["event"]["phase"] == "ready"


def test_tool_pack_websocket_heartbeat_and_fail_closed_auth(tmp_path, monkeypatch):
    platform = _platform(tmp_path)
    app = create_app(
        platform.store,
        artifact_store=platform.artifact_store,
        auth_token=TOKEN,
        tool_platform=platform,
    )
    monkeypatch.setattr(api_module, "TOOL_PACK_EVENT_POLL_SECONDS", 0.001)
    monkeypatch.setattr(api_module, "TOOL_PACK_EVENT_HEARTBEAT_TICKS", 1)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as unauthorized:
            with client.websocket_connect("/api/v1/tool-packs/events/ws"):
                pass
        assert unauthorized.value.code == 4401

        with client.websocket_connect(
            "/api/v1/tool-packs/events/ws",
            headers=AUTH,
        ) as websocket:
            assert websocket.receive_json() == {
                "kind": "replay_complete",
                "after_sequence": 0,
                "oldest_sequence": 1,
                "latest_sequence": 0,
                "truncated": False,
            }
            assert websocket.receive_json() == {
                "kind": "heartbeat",
                "after_sequence": 0,
                "oldest_sequence": 1,
                "latest_sequence": 0,
            }

    absent_store = NebulaStore(tmp_path / "absent.db")
    with TestClient(create_app(absent_store, auth_token=TOKEN)) as client:
        with pytest.raises(WebSocketDisconnect) as unavailable:
            with client.websocket_connect("/api/v1/tool-packs/events/ws", headers=AUTH):
                pass
        assert unavailable.value.code == 4501
