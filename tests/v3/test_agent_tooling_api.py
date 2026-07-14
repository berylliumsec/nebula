import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    AgentRun,
    Approval,
    Engagement,
    EngagementToolAssignment,
    ModelCapabilities as ProfileCapabilities,
    ProviderCapabilityVerification,
    ProviderProfile,
    ProviderVerificationStatus,
    RiskClass,
    RunStatus,
    ScopePolicy,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    ToolPackTrust,
    utc_now,
)
from nebula.v3.missions import MissionComponents, MissionService
from nebula.v3.orchestration import (
    SpecialistRole,
    StaticSpecialist,
    StaticSupervisor,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_platform import ToolPlatform
from nebula.v3.toolpacks import compile_manifest_yaml, manifest_digest


AUTH = {"Authorization": "Bearer test-token"}
PACK_DIGEST = "a" * 64
TOOL_NAME = "nmap.connect_scan"


def _create_engagement(client: TestClient, name: str = "Tooling API") -> dict:
    response = client.post(
        "/api/v1/engagements",
        headers=AUTH,
        json={"name": name},
    )
    assert response.status_code == 201
    return response.json()


def _runner_payload(**changes) -> dict:
    payload = {
        "name": "Rootless Podman",
        "runtime": "podman",
        "executable": "/usr/bin/podman",
        "socket": "unix:///run/user/1000/podman/podman.sock",
        "platform": "linux/amd64",
        "isolation": "rootless",
        "enabled": True,
    }
    payload.update(changes)
    return payload


def _installation(
    *,
    digest: str = PACK_DIGEST,
    status: ToolPackInstallationStatus = ToolPackInstallationStatus.READY,
) -> ToolPackInstallation:
    verified_at = utc_now() if status == ToolPackInstallationStatus.READY else None
    return ToolPackInstallation(
        publisher="berylliumsec",
        name=f"network-{digest[:8]}",
        version="1.0.0",
        manifest_digest=digest,
        source="test-fixture",
        trust=ToolPackTrust.CURATED,
        runtime_profile_id="runner-test",
        image_locks={
            "linux/amd64": f"ghcr.io/berylliumsec/nebula-tools/nmap@sha256:{digest}"
        },
        status=status,
        manifest_path=f"/tmp/{digest}.json",
        installed_at=utc_now(),
        verified_at=verified_at,
    )


def _wait_for_status(
    client: TestClient, run_id: str, expected: str, *, timeout: float = 5
) -> dict:
    deadline = time.monotonic() + timeout
    response = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=AUTH)
        assert response.status_code == 200
        if response.json()["status"] == expected:
            return response.json()
        time.sleep(0.01)
    actual = response.json()["status"] if response is not None else "not requested"
    raise AssertionError(f"run {run_id} did not reach {expected}; status was {actual}")


class StubProvider(ModelProvider):
    def __init__(self, profile: ProviderProfile):
        capabilities = ModelCapabilities(
            tools=profile.capabilities.tool_calling,
            strict_tools=profile.capabilities.strict_structured_output,
            structured_output=profile.capabilities.strict_structured_output,
        )
        super().__init__(
            ProviderConfig(
                id=profile.id,
                kind=ProviderKind.OPENAI_COMPATIBLE,
                flavor=ProviderFlavor.VLLM,
                base_url=profile.endpoint or "http://127.0.0.1:8000/v1",
                default_model="security-model",
                model_allowlist=profile.model_allowlist,
                local=True,
                enabled=profile.enabled,
                capabilities=capabilities,
            )
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise AssertionError("the deterministic runtime must not call a model")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


class RecordingRuntime:
    def __init__(self, store: NebulaStore, record: dict, *, request_approval: bool):
        self.store = store
        self.record = record
        self.request_approval = request_approval

    async def start(
        self,
        *,
        engagement_id: str,
        run_id: str,
        context: dict,
        **kwargs,
    ) -> dict:
        del kwargs
        self.record["start_context"] = context
        if self.request_approval:
            approval = self.store.create(
                Approval(
                    engagement_id=engagement_id,
                    run_id=run_id,
                    risk_class=RiskClass.ACTIVE_SCAN,
                    exact_request={
                        "tool_name": TOOL_NAME,
                        "arguments": {"target": "192.0.2.10", "ports": [443]},
                        "image_digest": PACK_DIGEST,
                    },
                    target="192.0.2.10",
                    expected_effects=["TCP connect scan of port 443"],
                    policy_rationale="active scans require operator approval",
                    requested_by="network-specialist",
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )
            self.record["approval_id"] = approval.id
            return {"__interrupt__": [{"kind": "tool_approval"}]}
        self._complete(run_id)
        return {"run_id": run_id, "status": "complete"}

    async def resume(self, run_id: str, response: dict) -> dict:
        self.record["resume_response"] = response
        self._complete(run_id)
        return {"run_id": run_id, "status": "complete"}

    def _complete(self, run_id: str) -> None:
        run = self.store.get(AgentRun, run_id)
        self.store.update(
            AgentRun,
            run.id,
            {"status": RunStatus.COMPLETE, "completed_at": utc_now()},
            expected_revision=run.revision,
        )


def _mission_app(
    tmp_path,
    *,
    request_approval: bool = False,
    executable_missions: bool = True,
):
    store = NebulaStore(tmp_path / "nebula.db")
    record: dict = {}

    @asynccontextmanager
    async def runtime_factory(**kwargs):
        assert kwargs["store"] is store
        record.setdefault("runtime_components", []).append(
            (kwargs["supervisor"], kwargs["specialists"])
        )
        yield RecordingRuntime(store, record, request_approval=request_approval)

    def components_factory(run: AgentRun, provider: ModelProvider):
        record["component_run_id"] = run.id
        record["component_provider_id"] = provider.config.id
        return MissionComponents(
            supervisor=StaticSupervisor(),
            specialists={SpecialistRole.NETWORK_SERVICE: StaticSpecialist()},
            context={"selected_tools": run.metadata["tool_names"]},
        )

    service = MissionService(
        store,
        checkpoint_path=tmp_path / "mission-checkpoints.db",
        provider_factory=StubProvider,
        runtime_factory=runtime_factory,
        tool_components_factory=components_factory,
    )
    app = create_app(
        store,
        auth_token="test-token",
        mission_service=service,
        enable_executable_missions=executable_missions,
    )
    return app, store, record


def _provider(store: NebulaStore, *, executable: bool = False) -> ProviderProfile:
    return store.create(
        ProviderProfile(
            name="Deterministic model",
            provider_type="vllm",
            endpoint="http://127.0.0.1:8000/v1",
            is_local=True,
            model_allowlist=["security-model"],
            capabilities=ProfileCapabilities(
                tool_calling=executable,
                strict_structured_output=executable,
            ),
            capability_verifications=(
                {
                    "security-model": ProviderCapabilityVerification(
                        model="security-model",
                        status=ProviderVerificationStatus.VERIFIED,
                    )
                }
                if executable
                else {}
            ),
        )
    )


def _mission_payload(engagement_id: str, provider_id: str, **changes) -> dict:
    payload = {
        "engagement_id": engagement_id,
        "objective": "Inspect only the explicitly authorized lab target",
        "provider_id": provider_id,
        "model": "security-model",
        "max_duration_seconds": 60,
        "max_tokens": 100,
        "max_retries": 0,
    }
    payload.update(changes)
    return payload


def _link_scope(store: NebulaStore, engagement: Engagement) -> ScopePolicy:
    scope = store.create(
        ScopePolicy(
            id=f"scope:{engagement.id}",
            engagement_id=engagement.id,
            allowed_cidrs=["192.0.2.0/24"],
            allowed_ports=[443],
            max_concurrency=2,
        )
    )
    store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": scope.id},
        expected_revision=engagement.revision,
    )
    return scope


def test_engagement_scope_is_synthesized_then_revisioned_on_update(tmp_path):
    store = NebulaStore(tmp_path / "scope.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    engagement = _create_engagement(client)

    default = client.get(f"/api/v1/engagements/{engagement['id']}/scope", headers=AUTH)

    assert default.status_code == 200
    assert default.json()["id"] == f"scope:{engagement['id']}"
    assert default.json()["engagement_id"] == engagement["id"]
    assert default.json()["allowed_cidrs"] == []
    assert default.json()["allowed_domains"] == []
    assert default.json()["allowed_urls"] == []
    assert default.json()["allowed_ports"] == []
    assert default.json()["local_only"] is False
    assert store.count(ScopePolicy, engagement_id=engagement["id"]) == 0

    created = client.put(
        f"/api/v1/engagements/{engagement['id']}/scope",
        headers=AUTH,
        json={
            "allowed_cidrs": ["192.0.2.19/24", "192.0.2.0/24"],
            "allowed_domains": ["API.Example.Test.", "api.example.test"],
            "allowed_urls": ["HTTPS://API.Example.Test"],
            "allowed_ports": [443, 80, 443],
            "prohibited_actions": ["credential_use"],
            "local_only": True,
            "max_concurrency": 2,
        },
    )

    assert created.status_code == 200
    assert created.json()["revision"] == 1
    assert created.json()["allowed_cidrs"] == ["192.0.2.0/24"]
    assert created.json()["allowed_domains"] == ["api.example.test"]
    assert created.json()["allowed_urls"] == ["https://api.example.test/"]
    assert created.json()["allowed_ports"] == [80, 443]
    assert (
        store.get(Engagement, engagement["id"]).scope_policy_id == created.json()["id"]
    )

    updated = client.put(
        f"/api/v1/engagements/{engagement['id']}/scope",
        headers=AUTH,
        json={
            "allowed_domains": ["www.example.test"],
            "allowed_ports": [8443],
            "expected_revision": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["allowed_domains"] == ["www.example.test"]

    stale = client.put(
        f"/api/v1/engagements/{engagement['id']}/scope",
        headers=AUTH,
        json={"allowed_ports": [443], "expected_revision": 1},
    )
    assert stale.status_code == 409


def test_new_engagement_inherits_ready_locally_trusted_pack(tmp_path):
    store = NebulaStore(tmp_path / "local-defaults.db")
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
    )
    manifest = compile_manifest_yaml(
        f"""api_version: tools.nebula.security/v1
kind: ToolPack
metadata:
  publisher: example
  name: local-default
  version: 1.0.0
  minimum_nebula_version: 3.0.0a1
  description: A locally trusted test pack.
  licenses: [BSD-3-Clause]
images:
  - name: sample
    platform: linux/amd64
    image: example.invalid/sample@sha256:{PACK_DIGEST}
    sbom: sample.cdx.json
    provenance: sample.intoto.jsonl
permissions: {{network: false, workspace: none, credentials: []}}
tools:
  - name: {TOOL_NAME}
    description: Run a local test capability.
    image: sample
    executable: /usr/bin/nmap
    input_schema: {{type: object, additionalProperties: false}}
    output_schema: {{type: object, additionalProperties: false}}
    policy: {{risk_class: local_read}}
    parser: {{built_in: json/v1}}
    smoke_tests:
      - arguments: {{}}
"""
    )
    digest = manifest_digest(manifest)
    manifest_path = platform.manifests.put(manifest)
    store.create(
        ToolPackInstallation(
            publisher=manifest.metadata.publisher,
            name=manifest.metadata.name,
            version=manifest.metadata.version,
            manifest_digest=digest,
            source="local-upload",
            trust=ToolPackTrust.LOCAL_TRUSTED,
            runtime_profile_id="runner-test",
            image_locks={"sample": manifest.images[0].image},
            status=ToolPackInstallationStatus.READY,
            manifest_path=str(manifest_path),
            installed_at=utc_now(),
            verified_at=utc_now(),
        )
    )
    client = TestClient(
        create_app(store, auth_token="test-token", tool_platform=platform)
    )

    engagement = _create_engagement(client, "Local pack defaults")

    [assignment] = store.list_entities(
        EngagementToolAssignment,
        engagement_id=engagement["id"],
    )
    assert assignment.manifest_digest == digest
    assert assignment.allowed_tool_names == [TOOL_NAME]
    assert assignment.enabled is True


def test_runner_profiles_validate_create_and_revisioned_update(tmp_path):
    store = NebulaStore(tmp_path / "runners.db")
    client = TestClient(create_app(store, auth_token="test-token"))

    created = client.put(
        "/api/v1/runner-profiles/local-podman",
        headers=AUTH,
        json=_runner_payload(),
    )
    assert created.status_code == 200
    assert created.json()["id"] == "local-podman"
    assert created.json()["revision"] == 1
    assert created.json()["healthy"] is False

    updated = client.put(
        "/api/v1/runner-profiles/local-podman",
        headers=AUTH,
        json=_runner_payload(name="Paused Podman", enabled=False, expected_revision=1),
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["name"] == "Paused Podman"
    assert updated.json()["enabled"] is False

    stale = client.put(
        "/api/v1/runner-profiles/local-podman",
        headers=AUTH,
        json=_runner_payload(expected_revision=1),
    )
    assert stale.status_code == 409

    invalid_payloads = [
        _runner_payload(executable="podman"),
        _runner_payload(socket="tcp://runner.example.test:2376"),
        _runner_payload(runtime="podman", executable="/usr/bin/docker"),
        _runner_payload(
            egress_helper_image="ghcr.io/berylliumsec/nebula-egress:latest"
        ),
        _runner_payload(healthy=True),
    ]
    for index, payload in enumerate(invalid_payloads):
        response = client.put(
            f"/api/v1/runner-profiles/invalid-{index}",
            headers=AUTH,
            json=payload,
        )
        assert response.status_code == 422, response.text


def test_tool_assignment_requires_a_ready_pack_and_is_revisioned(tmp_path):
    store = NebulaStore(tmp_path / "assignments.db")
    client = TestClient(create_app(store, auth_token="test-token"))
    engagement = _create_engagement(client)

    for index, status in enumerate(
        (
            ToolPackInstallationStatus.PENDING,
            ToolPackInstallationStatus.PULLING,
            ToolPackInstallationStatus.VERIFYING,
            ToolPackInstallationStatus.FAILED,
            ToolPackInstallationStatus.DISABLED,
        ),
        start=1,
    ):
        digest = f"{index:064x}"
        store.create(_installation(digest=digest, status=status))
        denied = client.put(
            f"/api/v1/engagements/{engagement['id']}/tool-assignment",
            headers=AUTH,
            json={"manifest_digest": digest, "tool_names": [TOOL_NAME]},
        )
        assert denied.status_code == 409
        assert "verified ready pack" in denied.json()["detail"]

    ready = store.create(_installation())
    assigned = client.put(
        f"/api/v1/engagements/{engagement['id']}/tool-assignment",
        headers=AUTH,
        json={
            "manifest_digest": ready.manifest_digest,
            "tool_names": [TOOL_NAME, TOOL_NAME, "nmap.service_scan"],
        },
    )
    assert assigned.status_code == 200
    assert assigned.json()["revision"] == 1
    assert assigned.json()["allowed_tool_names"] == [
        TOOL_NAME,
        "nmap.service_scan",
    ]

    updated = client.put(
        f"/api/v1/engagements/{engagement['id']}/tool-assignment",
        headers=AUTH,
        json={
            "manifest_digest": ready.manifest_digest,
            "tool_names": [TOOL_NAME],
            "enabled": False,
            "expected_revision": 1,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["enabled"] is False

    listed = client.get(
        f"/api/v1/engagements/{engagement['id']}/tool-assignment", headers=AUTH
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [updated.json()["id"]]


def test_analysis_mission_defaults_remain_non_executable(tmp_path):
    app, store, record = _mission_app(tmp_path)
    engagement = store.create(Engagement(name="Analysis only"))
    provider = _provider(store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id),
        )
        assert response.status_code == 202
        queued = response.json()
        assert queued["metadata"]["analysis_only"] is True
        assert queued["metadata"].get("tool_names", []) == []
        assert queued["tool_pack_digests"] == []
        assert queued["budget"]["max_tool_calls"] == 0
        assert queued["budget"]["max_concurrency"] == 1
        assert queued["budget"]["max_delegation_depth"] == 0
        _wait_for_status(client, queued["id"], "complete")

    assert "component_run_id" not in record


def test_public_api_keeps_executable_missions_release_gated_by_default(tmp_path):
    app, _store, _record = _mission_app(tmp_path, executable_missions=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(
                "not-evaluated",
                "not-evaluated",
                tool_names=[TOOL_NAME],
                max_tool_calls=1,
            ),
        )
    assert response.status_code == 409
    assert "release-gated" in response.json()["detail"]


def test_executable_mission_preflight_is_strict_and_pins_ready_packs(tmp_path):
    app, store, record = _mission_app(tmp_path)
    engagement = store.create(Engagement(name="Executable mission"))
    provider = _provider(store, executable=False)
    executable = {
        "tool_names": [TOOL_NAME],
        "max_tool_calls": 20,
        "max_concurrency": 2,
    }

    with TestClient(app) as client:
        unsupported = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id, **executable),
        )
        assert unsupported.status_code == 422
        assert "strict structured tool calling" in unsupported.json()["detail"]

        provider = store.update(
            ProviderProfile,
            provider.id,
            {
                "capabilities": ProfileCapabilities(
                    tool_calling=True,
                    strict_structured_output=True,
                ),
                "capability_verifications": {
                    "security-model": ProviderCapabilityVerification(
                        model="security-model",
                        status=ProviderVerificationStatus.VERIFIED,
                    )
                },
            },
            expected_revision=provider.revision,
        )
        missing_scope = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id, **executable),
        )
        assert missing_scope.status_code == 422
        assert "engagement scope policy" in missing_scope.json()["detail"]

        _link_scope(store, engagement)
        missing_assignment = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id, **executable),
        )
        assert missing_assignment.status_code == 422
        assert "not assigned" in missing_assignment.json()["detail"]

        installation = store.create(
            _installation(status=ToolPackInstallationStatus.VERIFYING)
        )
        store.create(
            EngagementToolAssignment(
                engagement_id=engagement.id,
                manifest_digest=installation.manifest_digest,
                allowed_tool_names=[TOOL_NAME],
                assigned_by="operator",
            )
        )
        unavailable = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id, **executable),
        )
        assert unavailable.status_code == 422
        assert "not verified and ready" in unavailable.json()["detail"]

        store.update(
            ToolPackInstallation,
            installation.id,
            {
                "status": ToolPackInstallationStatus.READY,
                "verified_at": utc_now(),
            },
            expected_revision=installation.revision,
        )
        accepted = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(engagement.id, provider.id, **executable),
        )
        assert accepted.status_code == 202
        queued = accepted.json()
        assert queued["metadata"] == {
            "analysis_only": False,
            "origin": "api",
            "tool_names": [TOOL_NAME],
        }
        assert queued["tool_pack_digests"] == [PACK_DIGEST]
        assert queued["budget"]["max_tool_calls"] == 20
        assert queued["budget"]["max_concurrency"] == 2
        assert queued["budget"]["max_delegation_depth"] == 1
        _wait_for_status(client, queued["id"], "complete")

    assert record["component_run_id"] == queued["id"]
    assert record["component_provider_id"] == provider.id
    assert record["start_context"] == {"selected_tools": [TOOL_NAME]}


def test_approval_decision_resumes_the_same_executable_run(tmp_path):
    app, store, record = _mission_app(tmp_path, request_approval=True)
    engagement = store.create(Engagement(name="Approval resume"))
    _link_scope(store, engagement)
    provider = _provider(store, executable=True)
    store.create(_installation())
    store.create(
        EngagementToolAssignment(
            engagement_id=engagement.id,
            manifest_digest=PACK_DIGEST,
            allowed_tool_names=[TOOL_NAME],
            assigned_by="operator",
        )
    )

    with TestClient(app) as client:
        started = client.post(
            "/api/v1/missions",
            headers=AUTH,
            json=_mission_payload(
                engagement.id,
                provider.id,
                tool_names=[TOOL_NAME],
                max_tool_calls=1,
            ),
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        waiting = _wait_for_status(client, run_id, "waiting_approval")
        assert waiting["metadata"]["waiting_approval"] is True

        decision = client.post(
            f"/api/v1/approvals/{record['approval_id']}/decision",
            headers=AUTH,
            json={"decision": "approve", "reason": "Authorized lab scan"},
        )
        assert decision.status_code == 200
        assert decision.json()["status"] == "approved"
        assert decision.json()["run_id"] == run_id
        assert decision.json()["decision_note"] == "Authorized lab scan"
        _wait_for_status(client, run_id, "complete")

    resume_response = record["resume_response"]
    assert resume_response["approval_id"] == record["approval_id"]
    assert resume_response["status"] == "approved"
    assert resume_response["decided_by"] == "system"
    assert datetime.fromisoformat(
        resume_response["decided_at"]
    ) == datetime.fromisoformat(decision.json()["decided_at"].replace("Z", "+00:00"))
    events = [event.event_type for event in store.replay_events(run_id)]
    assert "approval.resolved" in events
    assert "run.approval_resume_queued" in events


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/engagements/missing/scope", None),
        ("put", "/api/v1/engagements/missing/scope", {}),
        ("get", "/api/v1/engagements/missing/tool-assignment", None),
        (
            "put",
            "/api/v1/engagements/missing/tool-assignment",
            {"manifest_digest": PACK_DIGEST},
        ),
        ("get", "/api/v1/tool-catalog", None),
        ("get", "/api/v1/tool-packs", None),
        ("get", "/api/v1/tools", None),
        (
            "post",
            "/api/v1/tool-packs/install",
            {"catalog_id": "nebula-toolbox", "runtime_profile_id": "runner-test"},
        ),
        (
            "post",
            "/api/v1/tool-collections/install",
            {
                "collection_id": "nebula-toolbox",
                "runtime_profile_id": "runner-test",
            },
        ),
        (
            "post",
            "/api/v1/tool-packs/install-local",
            {
                "bundle_base64": "e30=",
                "runtime_profile_id": "runner-test",
                "developer_mode_confirmed": True,
            },
        ),
        ("post", "/api/v1/tool-packs/missing/verify", None),
        ("post", "/api/v1/tool-packs/missing/update", None),
        ("delete", "/api/v1/tool-packs/missing", None),
        ("get", "/api/v1/runner-profiles", None),
        ("put", "/api/v1/runner-profiles/test", _runner_payload()),
        ("post", "/api/v1/missions", {}),
        ("post", "/api/v1/approvals/missing/decision", {"decision": "approve"}),
    ],
)
def test_agent_tooling_endpoints_require_auth(tmp_path, method, path, payload):
    store = NebulaStore(tmp_path / "auth.db")
    client = TestClient(create_app(store, auth_token="test-token"))

    response = client.request(method, path, json=payload)

    assert response.status_code == 401
