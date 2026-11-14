"""Host Settings uses durable receipts around inert external adapters."""

import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from nebula.v3.credentials import CredentialStore
from nebula.v3.database import (
    DeploymentHostRow,
    EntityRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
)
from nebula.v3.deployment_protocol import GLOBAL_EXECUTION_PROJECT
from nebula.v3.domain import (
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    PairedDeviceSession,
    ProviderProfile,
    RunnerProfile,
    utc_now,
)
from nebula.v3.model_catalog import ModelDescriptor
from nebula.v3.providers import ModelResponse, ProviderError, ProviderHealth, ToolCall
from nebula.v3.runtime_host_delivery import HostEffectDelivery
from nebula.v3.runtime_delivery import HttpIntent
from nebula.v3.runtime_global_api import RuntimeGlobalApi
from nebula.v3.runtime_global_delivery import GlobalSqlDelivery
from nebula.v3.supervisor import RuntimeSupervisor
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import _TOKEN, managed as managed

pytestmark = pytest.mark.anyio
AUTH = {"Authorization": "Bearer " + _TOKEN}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MemoryKeyring:
    __module__ = "keyring.backends.SecretService"
    priority = 1

    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[service, key] = value

    def get_password(self, service, key):
        return self.values.get((service, key))

    def delete_password(self, service, key):
        self.values.pop((service, key), None)


@pytest.fixture
async def settings(managed):
    root = RuntimeSupervisor(managed.host, operator_token=_TOKEN)
    host = root.shared_services.host_settings
    host.credentials = CredentialStore(MemoryKeyring())
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()
    release.set()

    class Provider:
        def __init__(self, profile):
            self.profile = profile

        async def health(self):
            assert managed.database.task_session() is None
            calls.append(("health", self.profile.id))
            entered.set()
            await release.wait()
            return ProviderHealth(
                provider_id=self.profile.id,
                healthy=True,
                models=["model-a", "unlisted"],
                model_descriptors=[
                    ModelDescriptor(id="model-a", name="A", context_window=32000)
                ],
                catalog_source="inert",
            )

        async def complete(self, request):
            assert managed.database.task_session() is None
            calls.append(("verify", self.profile.id))
            nonce = request.tools[0].input_schema["properties"]["nonce"]["enum"][0]
            return ModelResponse(
                provider_id=self.profile.id,
                model=request.model,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="probe",
                        name="nebula_capability_probe",
                        arguments={"nonce": nonce},
                    )
                ],
            )

    host.provider_factory = Provider
    await root.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=root.app),
            base_url="https://fixture",
            headers=AUTH,
        ) as client:

            async def mutate(path, *, body=None, key="change", method="POST"):
                return await client.request(
                    method,
                    "/api/v1/" + path,
                    json=body,
                    headers={"Idempotency-Key": key},
                )

            yield SimpleNamespace(
                root=root,
                host=host,
                client=client,
                mutate=mutate,
                store=managed.store,
                managed=managed,
                calls=calls,
                entered=entered,
                release=release,
            )
    finally:
        release.set()
        await root.close()


async def provider(settings):
    return await settings.store.create(
        ProviderProfile(
            id="provider",
            name="Inert provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
        )
    )


async def test_host_health_and_capability_results_replay_without_network_or_project_owner(
    settings,
):
    await provider(settings)
    first, retry = await asyncio.gather(
        settings.mutate("providers/provider/health", key="health"),
        settings.mutate("providers/provider/health", key="health"),
    )
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert first.headers["x-nebula-command-id"] == retry.headers["x-nebula-command-id"]
    assert settings.calls == [("health", "provider")]
    assert first.json()["models"] == ["model-a"] and first.json()[
        "unlisted_models"
    ] == ["unlisted"]
    saved = await settings.store.get(ProviderProfile, "provider")
    checked = await settings.mutate(
        "providers/provider/capabilities/verify",
        body={"model": "model-a", "expected_revision": saved.revision},
        key="verify",
    )
    assert (
        checked.status_code == 200
        and checked.json()["verification"]["status"] == "verified"
    )
    stale = await settings.mutate(
        "providers/provider/capabilities/verify",
        body={"model": "model-a", "expected_revision": 1},
        key="stale",
    )
    assert (
        stale.status_code == 409 and settings.calls.count(("verify", "provider")) == 1
    )
    command = first.headers["x-nebula-command-id"]
    lookup = await settings.client.get(
        "/api/v1/runtime/requests/lookup", params={"idempotency_key": "health"}
    )
    assert (
        lookup.json()["command_id"] == command and lookup.json()["state"] == "succeeded"
    )
    replay = await settings.client.get(f"/api/v1/runtime/requests/{command}/response")
    assert replay.json() == first.json()
    assert (
        first.headers["x-nebula-request-state"]
        == replay.headers["x-nebula-request-state"]
        == "complete"
    )
    async with settings.managed.database.session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ProjectOwnershipRow))
            == 0
        )
        assert await session.get(EntityRow, GLOBAL_EXECUTION_PROJECT) is None


async def test_credentials_are_write_only_encrypted_and_shared_by_host_probes(settings):
    secret = "synthetic-credential-never-plaintext"
    created = await settings.mutate(
        "credentials", key="secret", body={"secret": secret, "persistence": "session"}
    )
    assert created.status_code == 201 and secret not in created.text
    reference = created.json()["reference"]
    again = await settings.mutate(
        "credentials", key="secret", body={"secret": secret, "persistence": "session"}
    )
    assert again.json() == created.json()
    assert (
        await settings.client.get(f"/api/v1/credentials/{reference}/status")
    ).json()["available"]
    assert settings.host.credentials.resolve(reference).get_secret_value() == secret
    await settings.store.create(
        ProviderProfile(
            id="credential-provider",
            name="Credential provider",
            provider_type="vllm",
            secret_ref=reference,
        )
    )
    factory = settings.host.provider_factory

    def credential_factory(profile):
        assert (
            settings.host.credentials.resolve(profile.secret_ref).get_secret_value()
            == secret
        )
        return factory(profile)

    settings.host.provider_factory = credential_factory
    checked = await settings.mutate(
        "providers/credential-provider/health", key="credential-health"
    )
    assert checked.status_code == 200 and secret not in checked.text
    async with settings.managed.database.session() as session:
        rows = list(await session.scalars(select(RuntimeCommandRow)))
        assert secret not in json.dumps([row.payload for row in rows])
    assert all(
        secret.encode() not in path.read_bytes()
        for path in settings.host.effect.delivery.vault.root.iterdir()
        if path.is_file()
    )
    deleted = await settings.mutate(
        f"credentials/{reference}", key="delete", method="DELETE"
    )
    assert deleted.status_code == 204
    assert (
        await settings.mutate(f"credentials/{reference}", key="delete", method="DELETE")
    ).status_code == 204
    assert not (
        await settings.client.get(f"/api/v1/credentials/{reference}/status")
    ).json()["available"]


async def test_runner_and_harness_probe_update_only_the_observed_profile(settings):
    from nebula.v3.harnesses import HarnessHealth

    runner = await settings.store.create(
        RunnerProfile(
            id="runner",
            name="Runner",
            runtime="podman",
            executable="/inert/podman",
            platform="linux/amd64",
            isolation="rootless",
        )
    )
    harness = await settings.store.create(
        HarnessProfile(
            id="harness",
            name="Harness",
            kind=HarnessKind.CODEX_APP_SERVER,
            executable="/inert/codex",
        )
    )

    async def available():
        return True, "Synthetic runner verified"

    settings.host.runner_factory = lambda _: SimpleNamespace(available=available)

    async def probe(profile, _credentials):
        return HarnessHealth(
            profile_id=profile.id,
            kind=profile.kind,
            healthy=True,
            capabilities=HarnessCapabilities(models=["discovered-model"]),
            authentication_state="verified",
        )

    settings.host.harness_factory = lambda _: SimpleNamespace(probe=probe)
    observed = await settings.mutate(f"runner-profiles/{runner.id}/probe", key="runner")
    assert observed.status_code == 200 and observed.json()["healthy"]
    checked = await settings.mutate(f"harnesses/{harness.id}/health", key="harness")
    assert (
        checked.status_code == 200
        and checked.json()["authentication_state"] == "verified"
    )
    saved = await settings.store.get(HarnessProfile, harness.id)
    assert (
        saved.default_model == "discovered-model"
        and saved.capabilities.session_state == "unverified"
    )


async def test_disconnected_viewer_can_lookup_and_replay_without_reexecuting(settings):
    await provider(settings)
    settings.release.clear()
    request = asyncio.create_task(
        settings.mutate("providers/provider/health", key="lost-response")
    )
    await asyncio.wait_for(settings.entered.wait(), 5)
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    lookup = await settings.client.get(
        "/api/v1/runtime/requests/lookup", params={"idempotency_key": "lost-response"}
    )
    assert lookup.json()["state"] == "running"
    identity = lookup.json()["command_id"]
    waiting = await settings.host.effect.response(
        "operator", identity, wait_seconds=0.01
    )
    assert waiting.status_code == 202
    assert waiting.headers["x-nebula-command-id"] == identity
    assert waiting.headers["x-nebula-request-state"] == "pending"
    settings.release.set()
    replay = await settings.client.get(f"/api/v1/runtime/requests/{identity}/response")
    assert replay.status_code == 200 and replay.json()["healthy"]
    assert replay.headers["x-nebula-request-state"] == "complete"
    assert settings.calls == [("health", "provider")]


async def test_restart_marks_unfinished_host_effect_uncertain_without_replay(settings):
    await provider(settings)
    settings.release.clear()
    intent = HttpIntent.capture(
        "POST", "/api/v1/providers/provider/health", b"", [], b""
    )
    first = await settings.host.effect.execute(
        intent, actor_id="operator", idempotency_key="unknown"
    )
    await asyncio.wait_for(settings.entered.wait(), 5)
    replacement = HostEffectDelivery(settings.host.effect.delivery, settings.host)
    await replacement.start()
    retry = await replacement.execute(
        intent, actor_id="operator", idempotency_key="unknown"
    )
    assert first == retry and settings.calls == [("health", "provider")]
    unresolved = await replacement.response("operator", first.command_id)
    assert unresolved.status_code == 409
    assert unresolved.headers["x-nebula-request-state"] == "needs-reconciliation"
    settings.release.set()
    await settings.host.effect.close()
    await replacement.close()
    status = await settings.client.get(f"/api/v1/runtime/requests/{first.command_id}")
    assert status.json()["state"] == "uncertain"


async def test_sql_global_commands_can_commit_while_host_effect_waits(settings):
    await provider(settings)
    settings.release.clear()
    pending = asyncio.create_task(
        settings.mutate("providers/provider/health", key="external")
    )
    await asyncio.wait_for(settings.entered.wait(), 5)
    app = RuntimeGlobalApi(settings.managed.database)
    try:
        sql = GlobalSqlDelivery(settings.host.effect.delivery, app)
        intent = HttpIntent.capture(
            "POST",
            "/api/v1/operator-profiles",
            b"",
            [(b"content-type", b"application/json")],
            b'{"display_name":"Synthetic operator"}',
        )
        receipt = await asyncio.wait_for(
            sql.execute(intent, actor_id="operator", idempotency_key="sql"), 3
        )
        assert receipt.sequence == 2
        settings.release.set()
        assert (await pending).status_code == 200
        async with settings.managed.database.session() as session:
            rows = list(
                await session.scalars(
                    select(RuntimeCommandRow).order_by(RuntimeCommandRow.sequence)
                )
            )
            assert [row.sequence for row in rows] == [1, 2]
    finally:
        settings.release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await app.close()


async def test_shutdown_guard_auth_and_changed_retry_do_not_admit_effects(settings):
    await provider(settings)
    unauthenticated = await settings.client.post(
        "/api/v1/providers/provider/health", headers={"Authorization": "Bearer invalid"}
    )
    assert unauthenticated.status_code == 401
    assert (
        await settings.client.post("/api/v1/providers/provider/health")
    ).status_code == 428
    saved = await settings.mutate("providers/provider/health", key="stable")
    assert saved.status_code == 200
    assert (
        await settings.mutate(
            "providers/provider/capabilities/verify",
            key="stable",
            body={"model": "model-a", "expected_revision": 2},
        )
    ).status_code == 409
    await settings.root.coordinator.status()
    async with settings.managed.database.session() as session:
        host = await session.get(DeploymentHostRow, "host")
        host.shutdown_requested = True
    assert (
        await settings.mutate("providers/provider/health", key="new")
    ).status_code == 409
    assert (
        await settings.mutate("providers/provider/health", key="stable")
    ).json() == saved.json()
    assert settings.calls == [("health", "provider")]


async def test_paired_settings_receipts_remain_private_to_the_accepting_device(
    settings,
):
    await provider(settings)
    now = utc_now()
    await settings.store.create(
        PairedDeviceSession(
            id="settings-viewer",
            name="Synthetic Settings viewer",
            token_sha256=hashlib.sha256(b"viewer-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"viewer-csrf").hexdigest(),
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=2),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=settings.root.app),
        base_url="https://fixture",
        cookies={"nebula_device": "viewer-token", "nebula_csrf": "viewer-csrf"},
        headers={"Origin": "https://fixture", "X-Nebula-CSRF": "viewer-csrf"},
    ) as viewer:
        checked = await viewer.post(
            "/api/v1/providers/provider/health",
            headers={"Idempotency-Key": "viewer-health"},
        )
        assert checked.status_code == 200, checked.text
        identity = checked.headers["X-Nebula-Command-Id"]
        for suffix in ("", "/response"):
            assert (
                await settings.client.get(
                    "/api/v1/runtime/requests/" + identity + suffix
                )
            ).status_code == 404
            assert (
                await viewer.get("/api/v1/runtime/requests/" + identity + suffix)
            ).status_code == 200
        assert (
            await settings.client.get(
                "/api/v1/runtime/requests/lookup",
                params={"idempotency_key": "viewer-health"},
            )
        ).status_code == 404
        lookup = await viewer.get(
            "/api/v1/runtime/requests/lookup",
            params={"idempotency_key": "viewer-health"},
        )
        assert lookup.json()["command_id"] == identity
        async with settings.managed.database.session() as session:
            assert (
                await session.get(RuntimeCommandRow, identity)
            ).actor_id == "device:settings-viewer"


async def test_fixed_provider_discovery_and_directory_use_injected_read_adapters(
    settings,
):
    seen = []

    async def directory(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(
            200, json={"data": [{"slug": "inert", "name": "Inert upstream"}]}
        )

    settings.host.directory_transport = httpx.MockTransport(directory)
    detected = await settings.client.get("/api/v1/providers/discover-local")
    assert detected.status_code == 200 and len(detected.json()) == 3
    assert all(item["endpoint"].startswith("http://") for item in detected.json())
    first = await settings.client.get("/api/v1/providers/openrouter/upstream-providers")
    again = await settings.client.get("/api/v1/providers/openrouter/upstream-providers")
    assert first.json() == again.json() and first.json()[0]["slug"] == "inert"
    assert len(seen) == 1 and seen[0][1] is None


async def test_batch_health_and_account_credits_use_host_adapters_once_per_receipt(
    settings,
):
    await provider(settings)
    result = await settings.mutate("provider-health/refresh", key="all-health")
    assert result.status_code == 200 and len(result.json()) == 1
    assert settings.calls == [("health", "provider")]
    assert (
        await settings.mutate("provider-health/refresh", key="all-health")
    ).json() == result.json()
    credential = await settings.mutate(
        "credentials",
        key="management-secret",
        body={"secret": "synthetic-management-only", "persistence": "session"},
    )
    await settings.store.create(
        ProviderProfile(
            id="credit-provider",
            name="Credits",
            provider_type="openrouter",
            endpoint="https://inference.invalid/v1",
            openrouter_management_secret_ref=credential.json()["reference"],
        )
    )
    requests = []

    def credit(request):
        assert settings.managed.database.task_session() is None
        assert str(request.url) == "https://openrouter.ai/api/v1/credits"
        assert request.headers["Authorization"] == "Bearer synthetic-management-only"
        requests.append(request.url)
        return httpx.Response(
            200, json={"data": {"total_credits": 10, "total_usage": 3}}
        )

    settings.host.credit_transport = httpx.MockTransport(credit)
    checked = await settings.mutate(
        "providers/credit-provider/openrouter/credits/refresh", key="credit"
    )
    assert checked.status_code == 200 and checked.json()["remaining_usd"] == 7
    assert "synthetic-management-only" not in checked.text
    replay = await settings.mutate(
        "providers/credit-provider/openrouter/credits/refresh", key="credit"
    )
    assert replay.json() == checked.json() and len(requests) == 1


def test_host_settings_import_does_not_load_the_execution_kernel():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nebula.v3.runtime_host_settings; assert 'nebula.v3.execution_kernel' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_host_browser_mode_and_ssh_discovery_follow_current_host_state(
    settings, monkeypatch
):
    monkeypatch.delenv("NEBULA_ONE_BROWSER_CUTOVER", raising=False)
    monkeypatch.delenv("NEBULA_AGENT_SECURITY_ENABLED", raising=False)
    before = await settings.client.get("/api/v1/desktop-browser-mode")
    assert before.json() == {
        "one_browser_cutover": False,
        "agent_security_enabled": False,
    }
    settings.root.shared_services.browser_bridge.enabled = True
    assert (await settings.client.get("/api/v1/desktop-browser-mode")).json()[
        "one_browser_cutover"
    ]
    resolves = []

    async def discovery(*, resolve):
        resolves.append(resolve)
        return {
            "config_path": "/inert/.ssh/config",
            "config_exists": True,
            "ssh_available": False,
            "hosts": [],
        }

    settings.host.ssh_discover = discovery
    assert (
        await settings.client.get(
            "/api/v1/ssh-environments", params={"resolve": "false"}
        )
    ).status_code == 200
    assert (
        await settings.client.get(
            "/api/v1/ssh-environments", params={"resolve": "true"}
        )
    ).status_code == 200
    assert resolves == [False, True]


async def test_saved_session_credential_serves_first_chat_in_an_actual_retained_kernel(
    settings,
):
    secret = "synthetic-session-credential-for-first-chat"
    created = await settings.mutate(
        "credentials",
        key="chat-secret",
        body={"secret": secret, "persistence": "session"},
    )
    assert created.status_code == 201
    source = str(Path(__file__).resolve().parents[2] / "src")
    digest = hashlib.sha256(secret.encode()).hexdigest()
    program = (
        "import sys\nsys.path.insert(0, "
        + repr(source)
        + ")\n"
        + r"""
import hashlib
from nebula.v3.providers import OpenAICompatibleProvider, ModelResponse, ModelStreamEvent, StreamEventType
async def complete(self, request):
    key = self.config.resolve_api_key().get_secret_value()
    if hashlib.sha256(key.encode()).hexdigest() != __DIGEST__:
        raise RuntimeError('Synthetic credential mismatch')
    return ModelResponse(provider_id=self.config.id, model=request.model, text='credential accepted', finish_reason='stop')
async def stream(self, request):
    yield ModelStreamEvent(type=StreamEventType.STARTED)
    response = await complete(self, request)
    yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta=response.text)
    yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)
OpenAICompatibleProvider.complete = complete
OpenAICompatibleProvider.stream = stream
def forbidden_network(*args, **kwargs):
    raise RuntimeError('Synthetic provider forbids real network calls')
OpenAICompatibleProvider._client = forbidden_network
import nebula.v3.execution_kernel as kernel
original = kernel.create_execution_kernel
def configured(*args, **kwargs):
    app = original(*args, **kwargs)
    app.state.control_assistant_work.chat.provider_scheduler.host_load = None
    return app
kernel.create_execution_kernel = configured
from nebula.v3.runtime_worker import main
main()
""".replace("__DIGEST__", repr(digest))
    )
    await settings.managed.stage("credential-v1", program)

    async def admission(_manifest, _role):
        return None

    settings.root.worker_admission = admission
    execution = await settings.root.prepare_execution("credential-v1")
    await settings.root.attach_execution("admitted-project", execution.runtime_id)
    api = await settings.root.prepare_api("credential-v1")
    await settings.root.promote_api(
        api.runtime_id,
        expected_revision=(await settings.root.coordinator.status()).revision,
    )
    configured = await settings.client.post(
        "/api/v1/providers",
        headers={"Idempotency-Key": "saved-provider"},
        json={
            "name": "Saved provider",
            "provider_type": "vllm",
            "is_local": True,
            "secret_ref": created.json()["reference"],
            "model_allowlist": ["model-a"],
            "metadata": {
                "options": {"context_window": 32000, "max_output_tokens": 1024}
            },
        },
    )
    assert configured.status_code == 201, configured.text
    response = await settings.client.post(
        "/api/v1/chat/completions",
        headers={"Idempotency-Key": "first-chat"},
        json={
            "provider_id": configured.json()["id"],
            "engagement_id": "admitted-project",
            "model": "model-a",
            "messages": [{"role": "user", "content": "Synthetic first chat"}],
            "include_knowledge": False,
            "stream": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"]["content"] == "credential accepted"
    assert secret not in response.text
    reference = created.json()["reference"]
    assert (
        await settings.mutate(
            f"credentials/{reference}", method="DELETE", key="remove-chat-secret"
        )
    ).status_code == 204
    refused = await settings.client.post(
        "/api/v1/chat/completions",
        headers={"Idempotency-Key": "after-delete"},
        json={
            "provider_id": configured.json()["id"],
            "engagement_id": "admitted-project",
            "model": "model-a",
            "messages": [{"role": "user", "content": "Credential was removed"}],
            "include_knowledge": False,
            "stream": False,
        },
    )
    assert refused.status_code >= 400 and secret not in refused.text


async def test_session_secret_rpc_requires_the_exact_lease_and_current_provider_reference(
    settings,
):
    from dataclasses import asdict

    from nebula.v3.execution_scope import execution_context
    from nebula.v3.runtime_coordination import LeaseLost, ProjectLease
    from nebula.v3.runtime_host_credentials import (
        RemoteSessionCredentials,
        SESSION_CREDENTIAL,
    )
    from nebula.v3.runtime_ipc import RuntimeIpcError
    from tests.v3.test_runtime_host_services import peer

    secret = "synthetic-exact-grant-secret"
    created = await settings.mutate(
        "credentials",
        key="rpc-secret",
        body={"secret": secret, "persistence": "session"},
    )
    reference = created.json()["reference"]
    profile = await settings.store.create(
        ProviderProfile(
            id="rpc-provider",
            name="RPC provider",
            provider_type="vllm",
            secret_ref=reference,
        )
    )
    await settings.managed.stage("rpc-v1")
    identity = await settings.managed.host.spawn("rpc-v1", "execution")
    lease = await settings.root.coordinator.coordination.claim_project(
        "admitted-project", identity.runtime_id
    )
    remote = await peer(settings.managed, identity)
    credentials = RemoteSessionCredentials(remote.client)
    try:
        with pytest.raises(LeaseLost):
            await credentials.resolve(profile.id, reference)
        with execution_context(lease):
            assert (
                await credentials.resolve(profile.id, reference)
            ).get_secret_value() == secret
            with pytest.raises(ProviderError, match="unavailable") as denied:
                await credentials.resolve(profile.id, "session:" + "0" * 32)
            assert secret not in str(denied.value)
        stale = ProjectLease(
            lease.project_id, lease.runtime_id, lease.fencing_token + 1
        )
        for invalid in (
            stale,
            ProjectLease(
                GLOBAL_EXECUTION_PROJECT, lease.runtime_id, lease.fencing_token
            ),
            ProjectLease(lease.project_id, "another-worker", lease.fencing_token),
        ):
            with pytest.raises(RuntimeIpcError):
                await remote.client.call(
                    SESSION_CREDENTIAL,
                    {
                        "lease": asdict(invalid),
                        "provider_id": profile.id,
                        "reference": reference,
                    },
                )
        await settings.store.update(
            ProviderProfile,
            profile.id,
            {"enabled": False},
            expected_revision=profile.revision,
        )
        with (
            execution_context(lease),
            pytest.raises(ProviderError, match="unavailable"),
        ):
            await credentials.resolve(profile.id, reference)
    finally:
        await remote.client.close()
