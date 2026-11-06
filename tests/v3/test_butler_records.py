"""Inventory and finding changes retain project, revision and attribution owners."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)


import pytest
from tests.v3.async_test_support import AsyncTestClient as TestClient

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import (
    Asset,
    Engagement,
    Evidence,
    Finding,
    OperatorProfile,
    utc_now,
)
from nebula.v3.relations import ResourceRelationService
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import CapabilityInputError


async def test_butler_records_discovery_is_project_scoped_and_bounded(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    foreign_asset = await store.create(
        Asset(engagement_id=other.id, name="Foreign inventory")
    )
    foreign_finding = await store.create(
        Finding(engagement_id=other.id, title="Foreign finding")
    )
    for index in range(52):
        (await store.create(Asset(engagement_id=project.id, name=f"Host {index}")))
    finding = await store.create(
        Finding(
            engagement_id=project.id,
            title="Bounded finding",
            description="x" * 9000,
            metadata={"private_extension": "not returned"},
        )
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def journey():
        categories = {
            item["id"] for item in (await capabilities.search({}))["categories"]
        }
        assert {"assets", "findings"} <= categories
        first = await capabilities.invoke(
            {"operation": "nebula.assets.list", "arguments": {}}, change=False
        )
        assert len(first["items"]) == 50 and first["next_offset"] == 50
        second = await capabilities.invoke(
            {"operation": "nebula.assets.list", "arguments": {"offset": 50}},
            change=False,
        )
        assert len(second["items"]) == 2 and second["next_offset"] is None
        assert foreign_asset.id not in str(first) + str(second)
        for operation, record in (
            ("nebula.assets.detail", foreign_asset),
            ("nebula.findings.detail", foreign_finding),
        ):
            with pytest.raises(ValueError, match="selected project"):
                await capabilities.invoke(
                    {"operation": operation, "arguments": {"record_id": record.id}},
                    change=False,
                )
        detail = await capabilities.invoke(
            {
                "operation": "nebula.findings.detail",
                "arguments": {"record_id": finding.id},
            },
            change=False,
        )
        assert (
            len(detail["description"]) == 8000
            and detail["next_description_offset"] == 8000
        )
        assert "private_extension" not in str(detail)
        tail = await capabilities.invoke(
            {
                "operation": "nebula.findings.detail",
                "arguments": {"record_id": finding.id, "description_offset": 8000},
            },
            change=False,
        )
        assert (
            len(tail["description"]) == 1000 and tail["next_description_offset"] is None
        )
        for operation in (
            "nebula.assets.create",
            "nebula.findings.create",
            "nebula.findings.update",
        ):
            schema = (await capabilities.describe({"operation": operation}))[
                "input_schema"
            ]
            assert (
                not {"engagement_id", "id", "metadata", "verifier_id", "verified_at"}
                & schema["properties"].keys()
            )

    (await journey())


async def test_butler_records_create_retry_and_revision_edit_use_core_validation(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    other = await store.create(Engagement(name="Other"))
    foreign = await store.create(Asset(engagement_id=other.id, name="Foreign"))
    evidence = await store.create(
        Evidence(
            engagement_id=project.id, title="Synthetic evidence", evidence_type="manual"
        )
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )
    client = TestClient(
        create_app(store, auth_token="test-token"),
        headers={"Authorization": "Bearer test-token"},
    )

    async def invoke(operation, arguments):
        return await capabilities.invoke(
            {"operation": operation, "arguments": arguments}, change=True
        )

    async def journey():
        asset_args = {
            "request_id": "asset-fixture-one",
            "name": "Inventory fixture",
            "asset_type": "host",
            "address": "192.0.2.20",
            "tags": ["synthetic"],
        }
        asset = await invoke("nebula.assets.create", asset_args)
        assert asset["read_back"] is True
        assert asset["client_action"] == {
            "kind": "records_refresh",
            "project_id": project.id,
            "resource": "assets",
        }
        asset_id = asset["result"]["id"]
        assert (await client.get(f"/api/v1/assets/{asset_id}")).json()[
            "name"
        ] == "Inventory fixture"
        retry = await invoke("nebula.assets.create", asset_args)
        assert retry["replayed"] is True and retry["result"]["id"] == asset_id
        with pytest.raises(ValueError, match="different record content"):
            await invoke("nebula.assets.create", {**asset_args, "name": "Different"})
        finding_args = {
            "request_id": "finding-fixture-one",
            "title": "Candidate fixture",
            "description": "An operator-supplied claim; no testing was started.",
            "severity": "low",
            "asset_ids": [asset_id],
            "evidence_ids": [evidence.id],
            "cve_ids": ["cve-2026-12345"],
        }
        created = await invoke("nebula.findings.create", finding_args)
        finding_id = created["result"]["id"]
        initial = (await client.get(f"/api/v1/findings/{finding_id}")).json()
        assert initial["status"] == "candidate" and initial["cve_ids"] == [
            "CVE-2026-12345"
        ]
        assert initial["asset_ids"] == [asset_id] and initial["evidence_ids"] == [
            evidence.id
        ]
        repeated = await invoke("nebula.findings.create", finding_args)
        assert repeated["replayed"] is True and repeated["result"]["id"] == finding_id
        with pytest.raises(ValueError):
            await invoke(
                "nebula.findings.create",
                {
                    **finding_args,
                    "request_id": "foreign-link-attempt",
                    "asset_ids": [foreign.id],
                },
            )
        assert len((await store.list_entities(Finding, engagement_id=project.id))) == 1
        saved = await invoke(
            "nebula.findings.update",
            {
                "record_id": finding_id,
                "expected_revision": initial["revision"],
                "title": "Reviewed candidate fixture",
                "severity": "medium",
                "cwe_ids": ["cwe-79"],
            },
        )
        assert (
            saved["read_back"] is True
            and saved["result"]["revision"] > initial["revision"]
        )
        observed = (await client.get(f"/api/v1/findings/{finding_id}")).json()
        assert observed["title"] == "Reviewed candidate fixture" and observed[
            "cwe_ids"
        ] == ["CWE-79"]
        assert observed["description"] == initial["description"] and observed[
            "evidence_ids"
        ] == [evidence.id]
        with pytest.raises(ValueError, match="current revision"):
            await invoke(
                "nebula.findings.update",
                {
                    "record_id": finding_id,
                    "expected_revision": initial["revision"],
                    "title": "Stale title",
                },
            )
        assert (await client.get(f"/api/v1/findings/{finding_id}")).json() == observed
        with pytest.raises(ValueError, match="current fields differ"):
            await invoke("nebula.findings.create", finding_args)
        assert len((await store.list_entities(Finding, engagement_id=project.id))) == 1

    (await journey())


async def test_butler_records_cannot_forge_verification_or_change_an_archived_project(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Selected"))
    operator = await store.create(OperatorProfile(display_name="Fixture operator"))
    evidence = await store.create(
        Evidence(
            engagement_id=project.id, title="Operator evidence", evidence_type="manual"
        )
    )
    confirmed = Finding(
        engagement_id=project.id,
        title="Operator confirmed",
        status="confirmed",
        evidence_ids=[evidence.id],
        verifier_id=operator.id,
        verified_at=utc_now(),
    )
    (await ResourceRelationService(store).create_legacy_entity(confirmed))
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), project.id
    )

    async def journey():
        with pytest.raises(ValueError, match="operator attribution"):
            await capabilities.invoke(
                {
                    "operation": "nebula.findings.update",
                    "arguments": {
                        "record_id": confirmed.id,
                        "expected_revision": confirmed.revision,
                        "title": "Changed attestation",
                    },
                },
                change=True,
            )
        with pytest.raises(CapabilityInputError) as rejected:
            await capabilities.invoke(
                {
                    "operation": "nebula.findings.create",
                    "arguments": {
                        "request_id": "forged-confirmation",
                        "title": "Unverified",
                        "status": "confirmed",
                        "verifier_id": operator.id,
                    },
                },
                change=True,
            )
        assert rejected.value.reason == "invalid_action_arguments"
        assert getattr(rejected.value, "_nebula_before_execution") is True
        assert (await store.get(Finding, confirmed.id)) == confirmed
        assert len((await store.list_entities(Finding, engagement_id=project.id))) == 1
        (
            await store.update(
                Engagement,
                project.id,
                {"status": "archived"},
                expected_revision=project.revision,
            )
        )
        with pytest.raises(ValueError, match="restore"):
            await capabilities.invoke(
                {
                    "operation": "nebula.assets.create",
                    "arguments": {
                        "request_id": "archived-creation",
                        "name": "No write",
                    },
                },
                change=True,
            )
        assert (await store.list_entities(Asset, engagement_id=project.id)) == []

    (await journey())


pytestmark = pytest.mark.anyio
