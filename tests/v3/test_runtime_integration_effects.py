"""Integration Settings writes use one canonical host and durable receipts."""

import asyncio
import json

import pytest
from sqlalchemy import select

from nebula.v3.database import EntityRow, RuntimeCommandRow, RuntimeResourceRow
from nebula.v3.domain import (
    AutomationProjectPolicy,
    AutomationSession,
    AutomationSessionStatus,
    ToolSuggestionSettings,
    ToolSuggestionTest,
    VpnProfile,
    WebSearchRuntimeState,
    WebSearchSettings,
    WebSearchTest,
    utc_now,
)
from tests.v3.test_runtime_host_settings import settings as settings
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_processes import managed as managed
from tests.v3.test_vpn import BASE

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def vpn_body(remote="vpn.example.com"):
    return {
        "name": "Synthetic VPN",
        "filename": "synthetic.ovpn",
        "config": BASE.replace("vpn.example.com", remote),
        "persistence": "session",
    }


async def test_vpn_create_replay_redacted_catalog_and_revision_delete(settings):
    body = vpn_body()
    created = await settings.mutate("vpn-profiles", key="vpn", body=body)
    assert created.status_code == 201 and created.json()["available"], created.text
    assert "secret_ref" not in created.json() and "certificate" not in created.text
    profile = await settings.store.get(VpnProfile, created.json()["id"])
    assert (
        await settings.mutate("vpn-profiles", key="vpn", body=body)
    ).json() == created.json()
    assert await settings.store.count(VpnProfile) == 1
    assert (
        await settings.mutate("vpn-profiles", key="vpn-duplicate", body=body)
    ).status_code == 409
    unsafe = vpn_body("unsafe.invalid")
    unsafe["config"] += "up /tmp/untrusted-script\n"
    assert (
        await settings.mutate("vpn-profiles", key="vpn-unsafe", body=unsafe)
    ).status_code == 422
    listed = await settings.client.get("/api/v1/vpn-profiles")
    assert listed.status_code == 200 and listed.json()[0]["id"] == profile.id
    path = f"vpn-profiles/{profile.id}"
    assert (
        await settings.mutate(
            path, method="DELETE", key="vpn-stale", body={"expected_revision": 99}
        )
    ).status_code == 409
    assert settings.host.credentials.status(profile.secret_ref).available
    removed = await settings.mutate(
        path,
        method="DELETE",
        key="vpn-delete",
        body={"expected_revision": profile.revision},
    )
    assert removed.status_code == 204
    assert (
        await settings.mutate(
            path,
            method="DELETE",
            key="vpn-delete",
            body={"expected_revision": profile.revision},
        )
    ).status_code == 204
    assert not settings.host.credentials.status(profile.secret_ref).available
    assert (await settings.client.get("/api/v1/vpn-profiles")).json() == []
    async with settings.managed.database.session() as session:
        payloads = [
            row.payload for row in await session.scalars(select(RuntimeCommandRow))
        ]
    assert "certificate" not in json.dumps(payloads)


async def test_vpn_delete_refuses_policy_active_command_and_uncertain_terminal_use(
    settings,
):
    created = await settings.mutate("vpn-profiles", key="bound-vpn", body=vpn_body())
    profile = await settings.store.get(VpnProfile, created.json()["id"])
    path, body = f"vpn-profiles/{profile.id}", {"expected_revision": profile.revision}
    policy = await settings.store.create(
        AutomationProjectPolicy(
            engagement_id="admitted-project", vpn_profile_id=profile.id
        )
    )
    blocked = await settings.mutate(
        path, method="DELETE", key="policy-block", body=body
    )
    assert blocked.status_code == 409 and "project command policies" in blocked.text
    await settings.store.delete(
        AutomationProjectPolicy, policy.id, expected_revision=policy.revision
    )
    active = await settings.store.create(
        AutomationSession(
            engagement_id="admitted-project",
            owner_kind="api",
            owner_id="synthetic",
            runtime_image="synthetic@sha256:" + "a" * 64,
            runtime_digest="sha256:" + "a" * 64,
            runner_profile_id="inert",
            runner_profile_revision=1,
            policy_id="old-policy",
            policy_revision=1,
            vpn_profile_id=profile.id,
            vpn_profile_revision=profile.revision,
            status=AutomationSessionStatus.READY,
        )
    )
    blocked = await settings.mutate(
        path, method="DELETE", key="session-block", body=body
    )
    assert blocked.status_code == 409 and "active command sessions" in blocked.text
    await settings.store.update(
        AutomationSession,
        active.id,
        {"status": AutomationSessionStatus.CLOSED},
        expected_revision=active.revision,
    )
    async with settings.managed.database.session() as session:
        session.add(
            RuntimeResourceRow(
                id="uncertain-terminal",
                project_id="admitted-project",
                runtime_id="unreachable-old",
                fencing_token=1,
                kind="resident_handle",
                state="uncertain",
                restoration="pinned",
                detail="terminal cleanup is uncertain",
                checkpoint={},
                updated_at_ms=0,
            )
        )
    blocked = await settings.mutate(
        path, method="DELETE", key="terminal-block", body=body
    )
    assert blocked.status_code == 409 and "reconcile active terminals" in blocked.text
    assert settings.host.credentials.status(profile.secret_ref).available


async def test_typesafe_save_test_replace_delete_and_encrypted_replay(
    settings, monkeypatch
):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    effects = settings.host.observations.integrations
    calls = []

    async def check(client):
        assert settings.managed.database.task_session() is None
        calls.append(client.api_key if client else None)
        return ToolSuggestionTest(
            tested_at=utc_now(),
            ok=client is not None,
            model="inert" if client else None,
            error=None if client else "No TypeSafe key is configured",
        )

    effects.typesafe_check = check
    body = {"secret": "synthetic-typesafe-first", "persistence": "session"}
    saved = await settings.mutate(
        "integrations/typesafe", method="PUT", key="jev-save", body=body
    )
    assert (
        saved.status_code == 200
        and saved.json()["last_test"]["ok"]
        and saved.json()["source"] == "session"
    ), saved.text
    first = await settings.store.get(ToolSuggestionSettings, "typesafe")
    assert body["secret"] not in saved.text and first.secret_ref not in saved.text
    assert (
        await settings.mutate(
            "integrations/typesafe", method="PUT", key="jev-save", body=body
        )
    ).json() == saved.json()
    assert calls == [body["secret"]]
    tested = await settings.mutate("integrations/typesafe/test", key="jev-test")
    assert tested.status_code == 200 and calls == [body["secret"]] * 2
    replacement = await settings.mutate(
        "integrations/typesafe",
        method="PUT",
        key="jev-replace",
        body={**body, "secret": "synthetic-typesafe-replacement"},
    )
    assert (
        replacement.status_code == 200
        and not settings.host.credentials.status(first.secret_ref).available
    )
    assert (
        await settings.client.get("/api/v1/integrations/typesafe")
    ).json() == replacement.json()
    async with settings.managed.database.session() as session:
        stored = list(await session.scalars(select(EntityRow)))
        assert all(
            "synthetic-typesafe-" not in json.dumps(row.payload) for row in stored
        )
    deleted = await settings.mutate(
        "integrations/typesafe", method="DELETE", key="jev-delete"
    )
    assert (
        deleted.status_code == 200
        and not deleted.json()["available"]
        and deleted.json()["last_test"] is None
    )
    assert (
        await settings.mutate(
            "integrations/typesafe", method="DELETE", key="jev-delete"
        )
    ).json() == deleted.json()


async def test_typesafe_rejected_probe_is_truthful_and_lost_response_does_not_repeat(
    settings,
):
    effects = settings.host.observations.integrations
    entered, release, calls = asyncio.Event(), asyncio.Event(), []

    async def check(client):
        calls.append(client.api_key)
        entered.set()
        await release.wait()
        return ToolSuggestionTest(
            tested_at=utc_now(), ok=False, error="Synthetic credential rejected"
        )

    effects.typesafe_check = check
    body = {"secret": "synthetic-rejected-key", "persistence": "session"}
    request = asyncio.create_task(
        settings.mutate(
            "integrations/typesafe", method="PUT", key="lost-jev", body=body
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        lookup = await settings.client.get(
            "/api/v1/runtime/requests/lookup", params={"idempotency_key": "lost-jev"}
        )
        assert lookup.status_code == 200 and lookup.json()["state"] == "running"
        release.set()
        response = await settings.client.get(
            "/api/v1/runtime/requests/" + lookup.json()["command_id"] + "/response"
        )
        assert response.status_code == 200 and not response.json()["last_test"]["ok"]
        assert response.json()["last_test"]["error"] == "Synthetic credential rejected"
        assert (
            await settings.mutate(
                "integrations/typesafe", method="PUT", key="lost-jev", body=body
            )
        ).json() == response.json()
        assert len(calls) == 1
    finally:
        release.set()


class InertSearch:
    def __init__(self, store):
        self.store, self.calls, self.current = store, [], WebSearchRuntimeState.ABSENT

    async def state(self):
        return self.current, "Synthetic search " + self.current.value

    async def record(self, **changes):
        self.calls.append(("record", changes))
        try:
            current = await self.store.get(WebSearchSettings, "web-search")
        except Exception as error:
            from nebula.v3.storage import NotFoundError

            if not isinstance(error, NotFoundError):
                raise
            return await self.store.create(
                WebSearchSettings(id="web-search", **changes)
            )
        return await self.store.update(
            WebSearchSettings, current.id, changes, expected_revision=current.revision
        )

    async def install(self):
        self.calls.append(("install", None))
        return "sha256:" + "b" * 64

    async def ensure_ready(self):
        self.calls.append(("start", None))
        self.current = WebSearchRuntimeState.READY
        await self.record(container_state=self.current, port=12345)
        return 12345

    async def stop(self):
        self.calls.append(("stop", None))
        self.current = WebSearchRuntimeState.STOPPED
        await self.record(container_state=self.current)

    async def remove(self):
        self.calls.append(("remove", None))
        self.current = WebSearchRuntimeState.ABSENT
        await self.record(container_state=self.current)

    async def check(self):
        self.calls.append(("test", None))
        return WebSearchTest(tested_at=utc_now(), ok=True)


async def test_supported_search_adapter_full_settings_lifecycle_and_replay(settings):
    observations = settings.host.observations
    runtime = observations.search = InertSearch(settings.store)
    observations.integrations.search_initialization_supported = True
    installed = await settings.mutate(
        "integrations/web-search/install", key="search-install"
    )
    assert (
        installed.status_code == 200
        and installed.json()["image_digest"] == "sha256:" + "b" * 64
    ), installed.text
    assert (
        await settings.mutate("integrations/web-search/install", key="search-install")
    ).json() == installed.json()
    started = await settings.mutate("integrations/web-search/start", key="search-start")
    assert started.status_code == 200 and started.json()["container_state"] == "ready"
    tested = await settings.mutate("integrations/web-search/test", key="search-test")
    assert tested.status_code == 200 and tested.json()["last_test"]["ok"]
    engines = await settings.mutate(
        "integrations/web-search/engines",
        method="PUT",
        key="search-engines",
        body={"engines": ["wikipedia"]},
    )
    assert engines.status_code == 200 and engines.json()["engines"] == ["wikipedia"]
    assert [name for name, _ in runtime.calls].count("remove") == 1
    assert [name for name, _ in runtime.calls].count("start") == 2
    stopped = await settings.mutate("integrations/web-search/stop", key="search-stop")
    assert stopped.status_code == 200 and stopped.json()["container_state"] == "stopped"
    removed = await settings.mutate(
        "integrations/web-search", method="DELETE", key="search-remove"
    )
    assert removed.status_code == 200 and removed.json()["container_state"] == "absent"
    assert (
        await settings.client.get("/api/v1/integrations/web-search")
    ).json() == removed.json()


async def test_unsupported_search_initialization_is_explicit_and_never_applied(
    settings,
):
    observations = settings.host.observations
    runtime = observations.search = InertSearch(settings.store)
    status = await settings.client.get("/api/v1/integrations/web-search")
    assert status.status_code == 200 and not status.json()["initialization_supported"]
    for action in ("install", "start", "test"):
        refused = await settings.mutate(
            "integrations/web-search/" + action, key="unsupported-" + action
        )
        assert refused.status_code == 501 and "memory-managed" in refused.text
    assert runtime.calls == []
    saved = await settings.mutate(
        "integrations/web-search/engines",
        method="PUT",
        key="offline-engines",
        body={"engines": ["wikipedia"]},
    )
    assert saved.status_code == 200 and saved.json()["engines"] == ["wikipedia"]
    runtime.current = WebSearchRuntimeState.READY
    calls = list(runtime.calls)
    refused = await settings.mutate(
        "integrations/web-search/engines",
        method="PUT",
        key="live-engines",
        body={"engines": ["brave"]},
    )
    assert refused.status_code == 501 and runtime.calls == calls


async def test_integration_mutation_auth_keys_and_validation_precede_effects(settings):
    runtime = settings.host.observations.search = InertSearch(settings.store)
    assert (
        await settings.client.put(
            "/api/v1/integrations/typesafe", json={"secret": "synthetic"}
        )
    ).status_code == 428
    assert (
        await settings.client.post(
            "/api/v1/vpn-profiles",
            headers={"Authorization": "Bearer invalid", "Idempotency-Key": "bad-auth"},
            json=vpn_body(),
        )
    ).status_code == 401
    for body in ({"engines": ["wikipedia", "wikipedia"]}, {"engines": ["unknown"]}):
        response = await settings.mutate(
            "integrations/web-search/engines",
            method="PUT",
            key="bad-engines-" + str(len(body["engines"])),
            body=body,
        )
        assert response.status_code == 422
    assert runtime.calls == []


async def test_dynamic_search_adapter_capabilities_are_checked_per_action(settings):
    observations = settings.host.observations
    runtime = observations.search = InertSearch(settings.store)

    async def capability(*, action="start"):
        return {
            "supported": action in {"start", "test"},
            "detail": "Synthetic per-action memory capability",
        }

    runtime.initialization_capability = capability
    status = await settings.client.get("/api/v1/integrations/web-search")
    assert (
        status.json()["initialization_supported"]
        and not status.json()["installation_supported"]
    )
    refused = await settings.mutate(
        "integrations/web-search/install", key="dynamic-install"
    )
    assert refused.status_code == 501 and runtime.calls == []
    started = await settings.mutate(
        "integrations/web-search/start", key="dynamic-start"
    )
    assert started.status_code == 200 and started.json()["container_state"] == "ready"
    before = list(runtime.calls)
    refused = await settings.mutate(
        "integrations/web-search/engines",
        method="PUT",
        key="dynamic-reconfigure",
        body={"engines": ["wikipedia"]},
    )
    assert refused.status_code == 501 and runtime.calls == before
