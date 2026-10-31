"""Butler evidence actions retain the project and requesting-device boundary."""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import Engagement, Evidence
from nebula.v3.storage import NebulaStore


def test_butler_evidence_pages_project_records_and_prepares_exact_file_handoffs(
    tmp_path,
):
    store = NebulaStore(tmp_path / "core.db")
    selected = store.create(Engagement(name="Selected"))
    foreign = store.create(Engagement(name="Foreign"))
    items = [
        store.create(
            Evidence(
                engagement_id=selected.id,
                evidence_type="operator_upload",
                title=f"Proof {index}",
                description="Private evidence description",
                artifact_id=f"artifact-{index}",
                sha256="a" * 64,
                metadata={"filename": f"proof-{index}.txt", "size": 10},
            )
        )
        for index in range(52)
    ]
    outsider = store.create(
        Evidence(
            engagement_id=foreign.id,
            evidence_type="operator_upload",
            title="Foreign proof",
            artifact_id="foreign-artifact",
            sha256="b" * 64,
        )
    )
    capabilities = ButlerCapabilities(
        store, ArtifactStore(tmp_path / "artifacts"), selected.id
    )

    async def invoke(name, arguments=None, *, change=False):
        return await capabilities.invoke(
            {"operation": f"nebula.evidence.{name}", "arguments": arguments or {}},
            change=change,
        )

    async def scenario():
        found = await capabilities.search({"query": "evidence original file"})
        assert any(
            row["operation"] == "nebula.evidence.download" for row in found["matches"]
        )
        assert (await capabilities.describe({"operation": "nebula.evidence.list"}))[
            "invoke_with"
        ] == "nebula.capabilities.read"
        assert (await capabilities.describe({"operation": "nebula.evidence.upload"}))[
            "invoke_with"
        ] == "nebula.capabilities.change"
        first = await invoke("list")
        assert len(first["evidence"]) == 50
        assert first["next_offset"] == 50
        assert len((await invoke("list", {"offset": 50}))["evidence"]) == 2
        assert outsider.id not in str(first)
        assert "Private evidence description" not in str(first)
        detail = await invoke("detail", {"evidence_id": items[0].id})
        assert detail["description"] == "Private evidence description"
        assert detail["revision"] == items[0].revision
        assert "artifact_id" not in detail
        with pytest.raises(ValueError, match="selected project"):
            await invoke("detail", {"evidence_id": outsider.id})
        upload = await invoke("upload", change=True)
        assert upload["client_action"] == {
            "kind": "evidence_upload",
            "project_id": selected.id,
        }
        download = await invoke(
            "download",
            {"evidence_id": items[0].id, "expected_revision": items[0].revision},
            change=True,
        )
        assert download["client_action"] == {
            "kind": "evidence_download",
            "project_id": selected.id,
            "evidence_id": items[0].id,
            "expected_revision": items[0].revision,
            "title": items[0].title,
            "expected_sha256": "a" * 64,
        }
        with pytest.raises(ValueError, match="current revision"):
            await invoke(
                "download",
                {"evidence_id": items[0].id, "expected_revision": 999},
                change=True,
            )
        with pytest.raises(ValueError, match="selected project"):
            await invoke(
                "download",
                {"evidence_id": outsider.id, "expected_revision": outsider.revision},
                change=True,
            )

    asyncio.run(scenario())
