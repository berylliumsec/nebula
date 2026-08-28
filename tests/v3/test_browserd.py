import asyncio
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from nebula.v3.browser_engine import BrowserEngineAction, BrowserEngineReceipt
from nebula.v3.browserd import (
    ActionLedger,
    BrowserdIdentityReceipt,
    BrowserdLifecycleReceipt,
    BrowserdManager,
    BrowserdSettings,
    create_browserd_app,
)
from nebula.v3.domain import BrowserEngineCapability, BrowserEngineState


def settings(tmp_path, *, headless: bool = False) -> BrowserdSettings:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return BrowserdSettings(
        token="opaque-browserd-token",
        profile_root=tmp_path / "profiles",
        policy_proxy_url="http://127.0.0.1:4911",
        runtime_root=runtime,
        headless=headless,
    )


def action(token: str = "action-1", *, url: str = "https://app.example.test/"):
    return BrowserEngineAction(
        action_token=token,
        assessment_id="assessment-1",
        session_id="session-1",
        identity_id="identity-1",
        tab_id="tab-1",
        kind="navigate",
        arguments={"url": url},
        side_effect="possible",
    )


def test_browserd_settings_require_a_literal_loopback_policy_proxy(tmp_path):
    with pytest.raises(ValueError, match="literal loopback"):
        BrowserdSettings(
            token="token",
            profile_root=tmp_path / "profiles",
            policy_proxy_url="http://proxy.example.test:4911",
            runtime_root=tmp_path / "runtime",
        )


def test_browserd_uploads_are_confined_to_an_explicit_host_browser(tmp_path):
    configured = settings(tmp_path)
    manager = BrowserdManager(configured)
    with pytest.raises(ValueError, match="not configured"):
        manager._upload_path(tmp_path / "outside.txt")

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    selected = upload_root / "selected.txt"
    selected.write_text("fixture", encoding="utf-8")
    bounded = BrowserdManager(
        BrowserdSettings(
            token=configured.token,
            profile_root=configured.profile_root,
            policy_proxy_url=configured.policy_proxy_url,
            runtime_root=configured.runtime_root,
            upload_root=upload_root,
        )
    )
    assert bounded._upload_path(str(selected)) == selected
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        bounded._upload_path(str(outside))


def test_action_ledger_deduplicates_receipts_and_marks_interrupted_work_ambiguous(
    tmp_path,
):
    async def exercise():
        ledger = ActionLedger(tmp_path / "ledger" / "actions.sqlite3")
        proposed = action()
        assert await ledger.claim(proposed) is None
        interrupted = await ledger.claim(proposed)
        assert interrupted is not None
        assert interrupted.state == "ambiguous"
        complete = BrowserEngineReceipt(
            action_token=proposed.action_token,
            state="complete",
            page_url="https://app.example.test/",
        )
        await ledger.finish(complete)
        assert await ledger.claim(proposed) == complete
        with pytest.raises(ValueError, match="different arguments"):
            await ledger.claim(action(url="https://app.example.test/changed"))

    asyncio.run(exercise())


class FakeBrowserdManager:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def readiness(self) -> BrowserEngineCapability:
        return BrowserEngineCapability(
            adapter="managed-chromium",
            display_name="Managed Chromium",
            state=BrowserEngineState.READY,
            installed_version="test",
            digest=f"sha256:{'a' * 64}",
            actions=["navigate"],
            protocols=["http", "https"],
        )

    async def ensure_identity(self, identity_id: str) -> BrowserdIdentityReceipt:
        return BrowserdIdentityReceipt(identity_id=identity_id, tab_ids=["tab-1"])

    async def execute(self, request: BrowserEngineAction) -> BrowserEngineReceipt:
        return BrowserEngineReceipt(
            action_token=request.action_token,
            state="complete",
            page_url="https://app.example.test/",
        )

    async def lifecycle(
        self, assessment_id: str, state: str
    ) -> BrowserdLifecycleReceipt:
        return BrowserdLifecycleReceipt(assessment_id=assessment_id, state=state)


def test_browserd_api_is_authenticated_and_exposes_normalized_receipts(tmp_path):
    configured = settings(tmp_path)
    manager = FakeBrowserdManager()
    with TestClient(
        create_browserd_app(configured, manager=manager)  # type: ignore[arg-type]
    ) as client:
        assert client.get("/v1/readiness").status_code == 401
        headers = {"Authorization": f"Bearer {configured.token}"}
        readiness = client.get("/v1/readiness", headers=headers)
        assert readiness.status_code == 200
        assert readiness.json()["state"] == "ready"
        identity = client.post(
            "/v1/identities/ensure",
            headers=headers,
            json={"identity_id": "identity-1"},
        )
        assert identity.json()["tab_ids"] == ["tab-1"]
        receipt = client.post(
            "/v1/actions", headers=headers, json=action().model_dump(mode="json")
        )
        assert receipt.status_code == 200
        assert receipt.json()["action_token"] == "action-1"
        stopped = client.post(
            "/v1/assessments/assessment-1/stop", headers=headers, json={}
        )
        assert stopped.json() == {
            "assessment_id": "assessment-1",
            "state": "stopped",
        }
    assert manager.started is True
    assert manager.closed is True


class FakeChromium:
    def __init__(self, executable_path: str) -> None:
        self.executable_path = executable_path


class FakePlaywright:
    def __init__(self, executable_path: str) -> None:
        self.chromium = FakeChromium(executable_path)

    async def stop(self) -> None:
        return None


class FakePlaywrightStarter:
    def __init__(self, executable_path: str) -> None:
        self.playwright = FakePlaywright(executable_path)

    async def start(self) -> FakePlaywright:
        return self.playwright


def test_browserd_readiness_requires_manifest_bound_full_chromium(tmp_path):
    configured = settings(tmp_path)
    executable = configured.runtime_root / "chromium-test" / "chrome-linux" / "chrome"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"verified full chromium")
    executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    (configured.runtime_root / "nebula-playwright-runtime.json").write_text(
        json.dumps(
            {
                "browser": "chromium",
                "playwright_version": "1.61.0",
                "executables": ["chromium-test/chrome-linux/chrome"],
                "executable_sha256": {
                    "chromium-test/chrome-linux/chrome": executable_sha256
                },
                "sbom": "nebula-playwright-sbom.spdx.json",
                "provenance": {"installer": "python -m playwright install chromium"},
            }
        ),
        encoding="utf-8",
    )
    manager = BrowserdManager(
        configured,
        playwright_factory=lambda: FakePlaywrightStarter(str(executable)),
    )

    async def exercise():
        await manager.start()
        capability = await manager.readiness()
        await manager.close()
        return capability

    capability = asyncio.run(exercise())
    assert capability.state == BrowserEngineState.READY
    assert capability.digest == f"sha256:{executable_sha256}"
    assert "cdp-screencast" in capability.protocols
