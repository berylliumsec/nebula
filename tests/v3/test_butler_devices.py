"""Butler can identify devices but leaves one-time and destructive effects to the browser."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Engagement, PairedDeviceSession, utc_now
from nebula.v3.storage import NebulaStore


def test_butler_devices_page_metadata_and_prepare_secret_free_browser_actions(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(Engagement(name="Device project"))
    now = utc_now()
    devices = [
        store.create(
            PairedDeviceSession(
                name=f"Device {index:02d}",
                token_sha256="a" * 64,
                csrf_sha256="b" * 64,
                idle_expires_at=now + timedelta(days=30),
                absolute_expires_at=now + timedelta(days=90),
            )
        )
        for index in range(51)
    ]
    revoked = store.create(
        PairedDeviceSession(
            name="Revoked device",
            token_sha256="c" * 64,
            csrf_sha256="d" * 64,
            idle_expires_at=now + timedelta(days=30),
            absolute_expires_at=now + timedelta(days=90),
            revoked_at=now,
        )
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.devices.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        discovered = await capabilities.search({"query": "pair revoke device"})
        assert any(
            row["operation"] == "nebula.devices.pair" for row in discovered["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.devices.list"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (await capabilities.describe({"operation": "nebula.devices.revoke"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        first = await invoke("list")
        assert len(first["devices"]) == 50
        assert first["next_offset"] == 50
        last = await invoke("list", {"offset": 50})
        assert len(last["devices"]) == 1
        assert revoked.id not in str(first) + str(last)
        assert "token_sha256" not in str(first)
        assert "csrf_sha256" not in str(first)
        pairing = await invoke("pair", {"name": " Field phone "}, change=True)
        assert pairing == {
            "state": "pending_browser_apply",
            "butler_operation": "nebula.devices.pair",
            "butler_project_id": project.id,
            "client_action": {
                "kind": "device_pair",
                "project_id": project.id,
                "name": "Field phone",
            },
        }
        with pytest.raises(ValueError, match="blank"):
            await invoke("pair", {"name": "   "}, change=True)
        target = devices[0]
        exact = {
            "device_id": target.id,
            "expected_name": target.name,
            "expected_created_at": target.created_at.isoformat(),
        }
        prepared = await invoke("revoke", exact, change=True)
        assert prepared == {
            "state": "pending_browser_apply",
            "butler_operation": "nebula.devices.revoke",
            "butler_project_id": project.id,
            "client_action": {
                "kind": "device_revoke",
                "project_id": project.id,
                **exact,
            },
        }
        assert store.get(PairedDeviceSession, target.id).revoked_at is None
        with pytest.raises(ValueError, match="identity changed"):
            await invoke("revoke", {**exact, "expected_name": "Other"}, change=True)
        with pytest.raises(ValueError, match="already revoked"):
            await invoke(
                "revoke",
                {
                    "device_id": revoked.id,
                    "expected_name": revoked.name,
                    "expected_created_at": revoked.created_at.isoformat(),
                },
                change=True,
            )

    asyncio.run(scenario())
