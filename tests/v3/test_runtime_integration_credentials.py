"""Canonical session keys reach scoped project consumers over the private socket."""

from dataclasses import asdict
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.automation_runtime import AutomationRuntimeManager
from nebula.v3.container_terminal import ContainerTerminalService
from nebula.v3.credentials import CredentialUnavailableError
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    ScopePolicy,
    ToolSuggestionSettings,
    ToolSuggestionTest,
    VpnProfile,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.runtime_coordination import LeaseLost, ProjectLease
from nebula.v3.runtime_credential_access import (
    credential_available_async,
    resolve_credential_async,
)
from nebula.v3.runtime_integration_credentials import (
    INTEGRATION_CREDENTIAL,
    RemoteExecutionCredentials,
)
from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.tool_suggestions import key_source, resolve_jev_client
from tests.v3.test_runtime_host_services import peer
from tests.v3.test_runtime_host_settings import MemoryKeyring, settings as settings
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_runtime_integration_effects import vpn_body

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def allow_typesafe(settings):
    project = await settings.store.get(Engagement, "admitted-project")
    scope = await settings.store.create(
        ScopePolicy(engagement_id=project.id, tool_suggestions=True)
    )
    await settings.store.update(
        Engagement,
        project.id,
        {"scope_policy_id": scope.id},
        expected_revision=project.revision,
    )
    return scope


async def save_typesafe(settings, secret="synthetic-project-typesafe"):
    async def check(client):
        return ToolSuggestionTest(tested_at=utc_now(), ok=client is not None)

    settings.host.observations.integrations.typesafe_check = check
    response = await settings.mutate(
        "integrations/typesafe",
        method="PUT",
        key="project-typesafe",
        body={"secret": secret, "persistence": "session"},
    )
    assert response.status_code == 200, response.text
    return await settings.store.get(ToolSuggestionSettings, "typesafe")


@pytest.fixture
async def credential_peer(settings):
    await settings.managed.stage("integration-rpc")
    identity = await settings.managed.host.spawn("integration-rpc", "execution")
    lease = await settings.root.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    client = (await peer(settings.managed, identity)).client
    try:
        yield SimpleNamespace(
            settings=settings,
            identity=identity,
            lease=lease,
            client=client,
            credentials=RemoteExecutionCredentials(client, MemoryKeyring()),
        )
    finally:
        await client.close()


async def test_typesafe_session_key_requires_opt_in_and_reads_current_host_reference(
    credential_peer, monkeypatch
):
    value = credential_peer
    settings = value.settings
    saved = await save_typesafe(settings)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-environment-fallback")
    with execution_context(value.lease):
        assert await key_source(settings.store, value.credentials) == ("session", True)
        assert await resolve_jev_client(settings.store, value.credentials) is None
        scope = await allow_typesafe(settings)
        client = await resolve_jev_client(settings.store, value.credentials)
        assert client.api_key == "synthetic-project-typesafe"
        await settings.store.update(
            ScopePolicy,
            scope.id,
            {"local_only": True},
            expected_revision=scope.revision,
        )
        assert await resolve_jev_client(settings.store, value.credentials) is None
        await settings.store.update(
            ScopePolicy,
            scope.id,
            {"local_only": False},
            expected_revision=scope.revision + 1,
        )
        assert (
            await resolve_jev_client(settings.store, value.credentials)
        ).api_key == "synthetic-project-typesafe"
        await settings.mutate(
            f"credentials/{saved.secret_ref}",
            method="DELETE",
            key="revoke-typesafe-session",
        )
        assert await resolve_jev_client(settings.store, value.credentials) is None
        assert await key_source(settings.store, value.credentials) == ("session", False)
    with pytest.raises(LeaseLost):
        await value.credentials.resolve_runtime(saved.secret_ref, purpose="typesafe")


async def test_vpn_preselection_reports_availability_but_use_requires_saved_project_policy(
    credential_peer,
):
    value, settings = credential_peer, credential_peer.settings
    created = await settings.mutate("vpn-profiles", key="project-vpn", body=vpn_body())
    profile = await settings.store.get(VpnProfile, created.json()["id"])
    terminal = ContainerTerminalService(
        store=settings.store, tool_platform=None, credential_store=value.credentials
    )
    automation = AutomationRuntimeManager(
        store=settings.store,
        artifact_store=ArtifactStore(settings.managed.data / "inert-artifacts"),
        data_root=settings.managed.data,
        workspace_resolver=lambda _: settings.managed.data,
        runtime_image="registry.invalid/nebula-automation@sha256:" + "a" * 64,
        credential_store=value.credentials,
    )
    try:
        with execution_context(value.lease):
            assert await credential_available_async(
                value.credentials,
                profile.secret_ref,
                purpose="vpn",
                profile_id=profile.id,
            )
            result = await value.client.call(
                INTEGRATION_CREDENTIAL,
                {
                    "lease": asdict(value.lease),
                    "purpose": "vpn",
                    "operation": "available",
                    "profile_id": profile.id,
                    "reference": profile.secret_ref,
                },
            )
            assert result["available"] and "secret" not in result
            with pytest.raises(CredentialUnavailableError):
                await resolve_credential_async(
                    value.credentials,
                    profile.secret_ref,
                    purpose="vpn",
                    profile_id=profile.id,
                )
            current = await automation.project_policy(value.lease.project_id)
            policy = await automation.update_project_policy(
                value.lease.project_id,
                approval_policy=current.approval_policy,
                network_enabled=True,
                runner_profile_id=None,
                vpn_profile_id=profile.id,
                max_timeout_ms=current.max_timeout_ms,
                expected_revision=current.revision,
            )
            selected, config = await terminal._selected_vpn(value.lease.project_id)
            assert selected.id == profile.id and "certificate" in config
            await settings.store.update(
                AutomationProjectPolicy,
                policy.id,
                {"vpn_profile_id": None},
                expected_revision=policy.revision,
            )
            with pytest.raises(CredentialUnavailableError):
                await resolve_credential_async(
                    value.credentials,
                    profile.secret_ref,
                    purpose="vpn",
                    profile_id=profile.id,
                )
    finally:
        await terminal.shutdown()
        await automation.shutdown()


async def test_integration_secret_rpc_rejects_stale_scope_changed_ref_and_wrong_purpose(
    credential_peer,
):
    value, settings = credential_peer, credential_peer.settings
    saved = await save_typesafe(settings)
    await allow_typesafe(settings)
    payload = {
        "lease": asdict(value.lease),
        "purpose": "typesafe",
        "operation": "resolve",
        "reference": saved.secret_ref,
    }
    stale = ProjectLease(
        value.lease.project_id, value.lease.runtime_id, value.lease.fencing_token + 1
    )
    for change in (
        {"lease": asdict(stale)},
        {"purpose": "provider"},
        {"profile_id": "another-profile"},
        {"reference": "session:" + "0" * 32},
        {"reference": "env:HOME"},
    ):
        with pytest.raises(RuntimeIpcError):
            await value.client.call(INTEGRATION_CREDENTIAL, {**payload, **change})
    await settings.store.update(
        ToolSuggestionSettings,
        saved.id,
        {"secret_ref": None},
        expected_revision=saved.revision,
    )
    with pytest.raises(RuntimeIpcError):
        await value.client.call(INTEGRATION_CREDENTIAL, payload)


async def test_local_credential_formats_preserve_environment_fallback(
    credential_peer, monkeypatch
):
    credentials = credential_peer.credentials
    monkeypatch.setenv("SYNTHETIC_RUNTIME_KEY", "synthetic-local-env")
    assert (
        await resolve_credential_async(
            credentials, "env:SYNTHETIC_RUNTIME_KEY", purpose="typesafe"
        )
    ).get_secret_value() == "synthetic-local-env"
    assert await credential_available_async(
        credentials, "env:SYNTHETIC_RUNTIME_KEY", purpose="typesafe"
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-env-typesafe")
    assert (
        await resolve_jev_client(credential_peer.settings.store, credentials)
    ).api_key == "synthetic-env-typesafe"


async def test_saved_integration_keys_serve_actual_retained_project_consumers(settings):
    secret = "synthetic-retained-typesafe"
    saved = await save_typesafe(settings, secret)
    await allow_typesafe(settings)
    created = await settings.mutate("vpn-profiles", key="retained-vpn", body=vpn_body())
    vpn = await settings.store.get(VpnProfile, created.json()["id"])
    policy_id = str(uuid5(NAMESPACE_URL, "nebula:automation-policy:admitted-project"))
    await settings.store.create(
        AutomationProjectPolicy(
            id=policy_id, engagement_id="admitted-project", vpn_profile_id=vpn.id
        )
    )
    vpn_text = settings.host.credentials.resolve(vpn.secret_ref).get_secret_value()
    source = str(Path(__file__).resolve().parents[2] / "src")
    program = (
        "import sys\nsys.path.insert(0, "
        + repr(source)
        + ")\n"
        + r"""
import hashlib
from nebula.v3.providers import OpenAICompatibleProvider, ModelResponse, ModelStreamEvent, StreamEventType
from nebula.v3.tool_suggestions import JevClient, resolve_jev_client
from nebula.v3.execution_scope import current_execution_lease
async def fixed_sample(self, state, questions):
    return {'model':'synthetic', 'answers':{key:{'noul':1.0} for key in questions}}
JevClient.system_one = fixed_sample
application = None
credentials = None
store = None
async def complete(self, request):
    client = await resolve_jev_client(store, credentials)
    if client is not None and hashlib.sha256(client.api_key.encode()).hexdigest() != __TYPE_DIGEST__:
        raise RuntimeError('Synthetic TypeSafe credential mismatch')
    selected = await application.state.container_terminal_service._selected_vpn(current_execution_lease().project_id)
    if selected is None or hashlib.sha256(selected[1].encode()).hexdigest() != __VPN_DIGEST__:
        raise RuntimeError('Synthetic VPN credential mismatch')
    return ModelResponse(provider_id=self.config.id, model=request.model, text='integrations accepted' if client else 'local fallback', finish_reason='stop')
async def stream(self, request):
    yield ModelStreamEvent(type=StreamEventType.STARTED)
    response = await complete(self, request)
    yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta=response.text)
    yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)
OpenAICompatibleProvider.complete = complete
OpenAICompatibleProvider.stream = stream
def forbidden_network(*args, **kwargs):
    raise RuntimeError('Synthetic integration test forbids provider network')
OpenAICompatibleProvider._client = forbidden_network
import nebula.v3.execution_kernel as kernel
original = kernel.create_execution_kernel
def configured(*args, **kwargs):
    global application, credentials, store
    credentials, store = kwargs['credential_store'], args[0]
    application = original(*args, **kwargs)
    application.state.control_assistant_work.chat.provider_scheduler.host_load = None
    return application
kernel.create_execution_kernel = configured
from nebula.v3.runtime_worker import main
main()
""".replace(
            "__TYPE_DIGEST__", repr(hashlib.sha256(secret.encode()).hexdigest())
        ).replace("__VPN_DIGEST__", repr(hashlib.sha256(vpn_text.encode()).hexdigest()))
    )
    await settings.managed.stage("integration-v1", program)

    async def admission(_manifest, _role):
        return None

    settings.root.worker_admission = admission
    execution = await settings.root.prepare_execution("integration-v1")
    await settings.root.attach_execution("admitted-project", execution.runtime_id)
    api = await settings.root.prepare_api("integration-v1")
    await settings.root.promote_api(
        api.runtime_id,
        expected_revision=(await settings.root.coordinator.status()).revision,
    )
    provider = await settings.client.post(
        "/api/v1/providers",
        headers={"Idempotency-Key": "integration-provider"},
        json={
            "name": "Inert integration provider",
            "provider_type": "vllm",
            "is_local": True,
            "model_allowlist": ["model-a"],
            "metadata": {
                "options": {"context_window": 32000, "max_output_tokens": 1024}
            },
        },
    )
    assert provider.status_code == 201, provider.text
    body = {
        "provider_id": provider.json()["id"],
        "engagement_id": "admitted-project",
        "model": "model-a",
        "messages": [{"role": "user", "content": "Use synthetic saved integrations"}],
        "include_knowledge": False,
        "stream": False,
    }
    first = await settings.client.post(
        "/api/v1/chat/completions",
        headers={"Idempotency-Key": "integration-chat"},
        json=body,
    )
    assert (
        first.status_code == 200
        and first.json()["message"]["content"] == "integrations accepted"
    ), first.text
    assert secret not in first.text and vpn_text not in first.text
    await settings.mutate(
        f"credentials/{saved.secret_ref}",
        method="DELETE",
        key="retained-typesafe-revoke",
    )
    second = await settings.client.post(
        "/api/v1/chat/completions",
        headers={"Idempotency-Key": "integration-after-revoke"},
        json=body,
    )
    assert (
        second.status_code == 200
        and second.json()["message"]["content"] == "local fallback"
    ), second.text
