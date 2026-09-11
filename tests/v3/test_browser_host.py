import asyncio

import pytest

from nebula.v3.browser_host import ManagedBrowserHost
from nebula.v3.storage import NebulaStore


def test_missing_host_bundle_is_recoverable_without_starting_listeners(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    host = ManagedBrowserHost(NebulaStore(tmp_path / "core.db"), tmp_path / "host")
    with pytest.raises(ValueError, match="saved conversations remain available"):
        asyncio.run(host.adapter())
    assert host._task is None and host._socket is None and not host._proxies
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="verification manifest"):
        asyncio.run(host.adapter())
    assert not (tmp_path / "host").exists()


def test_failed_host_preparation_releases_private_listeners(tmp_path, monkeypatch):
    from nebula.v3 import browser_host

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":fixture")
    (tmp_path / "nebula-playwright-runtime.json").write_text("{}")

    def unavailable(*args, **kwargs):
        raise OSError("Controlled unavailable profile directory")

    monkeypatch.setattr(browser_host, "BrowserdManager", unavailable)
    host = ManagedBrowserHost(NebulaStore(tmp_path / "core.db"), tmp_path / "host")
    with pytest.raises(OSError, match="Controlled unavailable"):
        asyncio.run(host.adapter())
    assert host._task is None and host._socket is None and not host._proxies


def test_identity_proxy_rechecks_durable_revocation_and_deletion(tmp_path):
    from nebula.v3.domain import BrowserIdentity, Engagement, ScopePolicy, utc_now

    async def exercise():
        store = NebulaStore(tmp_path / "core.db")
        project = store.create(Engagement(name="Host scope"))
        scope = store.create(
            ScopePolicy(engagement_id=project.id, allowed_domains=["allowed.test"])
        )
        store.update(Engagement, project.id, {"scope_policy_id": scope.id})
        identity = store.create(
            BrowserIdentity(engagement_id=project.id, name="Host identity")
        )
        host = ManagedBrowserHost(store, tmp_path / "host")
        try:
            await host._identity_proxy(identity.id)
            proxy = host._proxies[identity.id]
            proxy.authorize("https://allowed.test/")
            with pytest.raises(ValueError):
                proxy.authorize("https://outside.test/")
            store.update(BrowserIdentity, identity.id, {"revoked_at": utc_now()})
            with pytest.raises(ValueError, match="revoked"):
                proxy.authorize("https://allowed.test/")
            store.delete(BrowserIdentity, identity.id)
            with pytest.raises(ValueError, match="unavailable"):
                proxy.authorize("https://allowed.test/")
        finally:
            await host.close()

    asyncio.run(exercise())


def test_partial_external_configuration_does_not_switch_browser_profiles(
    tmp_path, monkeypatch
):
    from nebula.v3.browser_companion import BrowserCompanion
    from nebula.v3.browser_engine import BrowserEngineRegistry

    monkeypatch.setenv("NEBULA_BROWSERD_URL", "http://127.0.0.1:4711")
    monkeypatch.delenv("NEBULA_BROWSERD_TOKEN", raising=False)

    class ForbiddenHost:
        async def adapter(self):
            pytest.fail("An explicitly configured browser must not switch profiles")

    host = ForbiddenHost()
    service = BrowserCompanion(
        NebulaStore(tmp_path / "core.db"), BrowserEngineRegistry(), managed_host=host
    )
    with pytest.raises(ValueError, match="Managed Chromium is unavailable"):
        asyncio.run(service.adapter())


def test_lazy_host_adapter_becomes_available_to_guided_assessments(tmp_path):
    from nebula.v3.browser_companion import BrowserCompanion
    from nebula.v3.browser_engine import LocalBrowserdAdapter, BrowserEngineRegistry
    from nebula.v3.domain import BrowserEngineCapability, BrowserEngineState

    class ReadyAdapter(LocalBrowserdAdapter):
        def __init__(self):
            super().__init__("http://127.0.0.1:4711", "fixture-token")

        async def readiness(self):
            return BrowserEngineCapability(
                adapter="managed-chromium",
                display_name="Managed Chromium",
                state=BrowserEngineState.READY,
                installed_version="fixture",
                digest=f"sha256:{'a' * 64}",
                actions=["navigate", "takeover"],
                protocols=["http", "https"],
            )

    adapter = ReadyAdapter()

    class Host:
        async def adapter(self):
            return adapter

    host = Host()
    registry = BrowserEngineRegistry([])
    companion = BrowserCompanion(
        NebulaStore(tmp_path / "core.db"), registry, managed_host=host
    )

    assert asyncio.run(companion.adapter()) is adapter
    assert asyncio.run(registry.adapter("managed-chromium")) is adapter
