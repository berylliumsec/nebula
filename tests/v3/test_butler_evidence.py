"""Butler prepares project evidence without receiving or storing device bytes."""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Asset, ButlerRecoveryCase, Engagement, Evidence, Finding
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError


def test_butler_evidence_pages_project_records_and_prepares_device_upload(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    finding = store.create(Finding(engagement_id=selected.id, title="Selected finding"))
    asset = store.create(Asset(engagement_id=selected.id, name="Selected asset"))
    foreign_finding = store.create(
        Finding(engagement_id=foreign.id, title="Foreign finding")
    )
    foreign_asset = store.create(Asset(engagement_id=foreign.id, name="Foreign asset"))
    foreign_evidence = store.create(
        Evidence(
            engagement_id=foreign.id,
            evidence_type="other",
            title="Foreign evidence",
            description="Foreign private bytes",
        )
    )
    records = [
        store.create(
            Evidence(
                engagement_id=selected.id,
                evidence_type="operator_upload",
                title=f"Evidence {index}",
                description="Private evidence description",
                metadata={"content": "private device bytes"},
            )
        )
        for index in range(52)
    ]
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )
    case = store.create(
        ButlerRecoveryCase(
            engagement_id=selected.id,
            trigger="immediate",
            signal="goal.blocked",
            source_event_id="synthetic-event",
            source_event_sequence=1,
            authority_revision=1,
            summary="Synthetic check",
        )
    )
    background = ButlerCapabilities(
        store,
        ArtifactStore(tmp_path / "background-artifacts"),
        selected.id,
        background_case_id=case.id,
    )

    async def invoke(operation, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.evidence.{operation}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        found = await capabilities.search({"query": "upload evidence file from device"})
        assert any(
            row["operation"] == "nebula.evidence.upload_from_device"
            for row in found["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.evidence.list"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (
            await capabilities.describe(
                {"operation": "nebula.evidence.upload_from_device"}
            )
        )["invoke_with"] == "nebula.capabilities.change"
        first = await invoke("list")
        assert len(first["evidence"]) == 50
        assert first["next_offset"] == 50
        assert len((await invoke("list", {"offset": 50}))["evidence"]) == 2
        assert foreign_evidence.id not in str(first)
        assert "private device bytes" not in str(first)
        detail = await invoke("detail", {"evidence_id": records[0].id})
        assert detail["evidence_id"] == records[0].id
        assert "private device bytes" not in str(detail)
        assert (await invoke("targets", {"kind": "finding"}))["items"][0][
            "id"
        ] == finding.id
        assert (await invoke("targets", {"kind": "asset"}))["items"][0][
            "id"
        ] == asset.id
        with pytest.raises(ValueError, match="outside the selected project"):
            await invoke("detail", {"evidence_id": foreign_evidence.id})
        with pytest.raises(
            CapabilityInputError, match="use nebula.capabilities.change"
        ):
            await invoke("upload_from_device")
        prepared = await invoke(
            "upload_from_device",
            {
                "title": "  Synthetic proof  ",
                "description": "Operator selected file",
                "evidence_type": "operator_upload",
                "finding_id": finding.id,
                "asset_ids": [asset.id],
            },
            change=True,
        )
        assert prepared == {
            "state": "pending_browser_apply",
            "butler_operation": "nebula.evidence.upload_from_device",
            "butler_project_id": selected.id,
            "client_action": {
                "kind": "evidence_upload",
                "project_id": selected.id,
                "title": "Synthetic proof",
                "description": "Operator selected file",
                "evidence_type": "operator_upload",
                "finding_id": finding.id,
                "asset_ids": [asset.id],
            },
        }
        assert store.count(Evidence) == 53
        for invalid in (
            {"finding_id": foreign_finding.id},
            {"asset_ids": [foreign_asset.id]},
        ):
            with pytest.raises(ValueError, match="outside the selected project"):
                await invoke("upload_from_device", invalid, change=True)
        with pytest.raises(CapabilityInputError):
            await background.describe(
                {"operation": "nebula.evidence.upload_from_device"}
            )

    asyncio.run(scenario())
