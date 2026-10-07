"""The Intel Atlas keeps file evidence separate from live Work status."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from nebula.v3 import intel_atlas
from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, EngagementStatus
from nebula.v3.storage import NebulaStore
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService


def _fixture(tmp_path):
    root = tmp_path / "research"
    focused = root / "projects" / "focused_research"
    supporting = root / "projects" / "supporting_research"
    focused.mkdir(parents=True)
    supporting.mkdir(parents=True)
    (supporting / "data" / "binaries").mkdir(parents=True)
    (supporting / "data" / "binaries" / "legacy.bin").write_bytes(b"old")
    (root / "GRAND_THREAT_PROJECT_MAP.md").write_text(
        "# Project Map\n\nSnapshot: 2026-08-25\n\n"
        "## Surface-to-project index\n\n"
        "| Grand threat surface | Focused project anchors | Supporting anchors | Current read |\n"
        "| --- | --- | --- | --- |\n"
        "| Remote input | [Focused](projects/focused_research) | "
        "[Supporting](projects/supporting_research) | Historic read. |\n",
        encoding="utf-8",
    )
    (focused / "artifacts" / "knowledge").mkdir(parents=True)
    (focused / "artifacts" / "knowledge" / "research_lanes.jsonl").write_text(
        json.dumps({"status": "open"}) + "\n", encoding="utf-8"
    )
    (focused / "artifacts" / "knowledge" / "simulation_runs.jsonl").write_text(
        json.dumps({"run_key": "sim-1", "status": "bounded", "feasibility": "unknown",
                    "decision_impact": "none", "summary": "Fixture run."}) + "\n", encoding="utf-8"
    )
    (focused / "data" / "binaries").mkdir(parents=True)
    (focused / "data" / "binaries" / "sample.bin").write_bytes(b"test")
    (focused / "project_metadata.json").write_text(
        json.dumps({"targets": [{"analysisBinaryPath":
                 "projects/focused_research/data/binaries/sample.bin"}]}), encoding="utf-8"
    )
    (focused / "artifacts" / "evidence").mkdir(parents=True)
    (focused / "artifacts" / "evidence" / "graph_manifest.json").write_text(
        json.dumps({"schema_version": "2.0", "mode": "shadow"}), encoding="utf-8"
    )
    (focused / "artifacts" / "evidence" / "investigation_nodes.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (focused / "artifacts" / "evidence" / "investigation_edges.jsonl").write_text(
        "", encoding="utf-8"
    )
    (root / "dashboard" / "data").mkdir(parents=True)
    (root / "dashboard" / "data" / "migration-statuses.json").write_text(
        json.dumps({"auditedAt": "2026-09-13T17:05:24Z", "projects": {
            "projects/focused_research": {"validation": "not_validated", "detail": "Audit is old.",
                                          "remainingGate": "Review graph."}}}), encoding="utf-8"
    )
    store = NebulaStore(tmp_path / "core.db")
    archived = store.create(Engagement(name="Old alias", status=EngagementStatus.ARCHIVED,
                                      workspace_path=str(focused)))
    active = store.create(Engagement(name="New alias", status=EngagementStatus.ACTIVE,
                                    workspace_path=str(focused)))
    work = WorkService(store)
    for engagement, summary in ((archived, "Old status"), (active, "Live status")):
        item = work.create(engagement.id, WorkCreate(title="Research status"), actor_id="fixture")
        work.check_in(engagement.id, item.id,
                      WorkCheckIn(summary=summary, next_step="Review next lane", status="in_progress"),
                      actor_kind="operator", actor_id="fixture")
    return root, store, active


def test_atlas_uses_linked_work_and_marks_missing_lane_evidence_unknown(tmp_path, monkeypatch):
    root, store, active = _fixture(tmp_path)
    client = TestClient(create_app(store, auth_token="atlas-test"))
    auth = {"Authorization": "Bearer atlas-test"}
    assert client.get("/api/v1/atlas").status_code == 401

    response = client.get("/api/v1/atlas", headers=auth)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["source"]["mapSnapshot"] == "2026-08-25"
    assert result["counts"]["grandThreatProjects"] == 2
    assert result["counts"]["openLanes"] is None  # supporting project has no index
    assert result["counts"]["simulations"] == 1
    assert result["simulations"][0]["runKey"] == "sim-1"
    projects = {project["name"]: project for project in result["grandThreatProjects"]}
    focused = projects["focused_research"]
    assert focused["workspace"]["engagementId"] == active.id
    assert focused["workflow"]["summary"] == "Live status"
    assert focused["workflow"]["source"] == "nebula-work"
    assert focused["lanes"]["open"] == 1
    assert focused["binaries"]["count"] == 1
    assert focused["v2Migration"]["status"] == "shadow_v2"
    assert focused["v2Migration"]["validation"] == "not_validated"
    assert focused["v2Migration"]["auditedAt"] == "2026-09-13T17:05:24Z"
    assert focused["migration"]["status"] == "linked"
    supporting = projects["supporting_research"]
    assert supporting["workflow"]["status"] == "unknown"
    assert supporting["lanes"]["open"] is None
    assert supporting["binaries"]["count"] == 1
    assert supporting["migration"]["status"] == "unlinked"

    linked_supporting = store.create(
        Engagement(name="Renamed supporting", status=EngagementStatus.ACTIVE,
                   workspace_path=str(root / "projects" / "supporting_research"))
    )
    refreshed = client.get("/api/v1/atlas", headers=auth).json()
    supporting = next(project for project in refreshed["grandThreatProjects"]
                      if project["name"] == "supporting_research")
    assert supporting["workspace"]["engagementId"] == linked_supporting.id
    assert supporting["workflow"]["status"] == "unknown"
    assert supporting["workflow"]["source"] == "unknown"
    WorkService(store).create(
        linked_supporting.id, WorkCreate(title="Research status"), actor_id="fixture"
    )
    refreshed = client.get("/api/v1/atlas", headers=auth).json()
    supporting = next(project for project in refreshed["grandThreatProjects"]
                      if project["name"] == "supporting_research")
    assert supporting["workflow"] == {
        "status": "backlog", "summary": "No current Research status check-in.",
        "nextStep": None, "updatedAt": None, "source": "nebula-work"
    }

    document = next(item for item in result["documents"] if item["path"] == "GRAND_THREAT_PROJECT_MAP.md")
    detail = client.get(f"/api/v1/atlas/documents/{document['id']}", headers=auth)
    assert detail.status_code == 200
    assert detail.json()["content"] == (root / "GRAND_THREAT_PROJECT_MAP.md").read_text()
    assert client.get("/api/v1/atlas/documents/../../etc/passwd", headers=auth).status_code in {404, 405}
    assert client.get("/api/v1/atlas/documents/000000000000", headers=auth).status_code == 404

    original_read = intel_atlas._read_text
    monkeypatch.setattr(
        intel_atlas, "_read_text",
        lambda path, limit: None if path.name == "GRAND_THREAT_PROJECT_MAP.md"
        else original_read(path, limit),
    )
    unavailable = client.get("/api/v1/atlas", headers=auth)
    assert unavailable.status_code == 200
    assert unavailable.json()["source"]["available"] is False


def test_atlas_status_refreshes_from_work_without_legacy_dashboard_store(tmp_path):
    root, store, active = _fixture(tmp_path)
    (root / "dashboard" / "data" / "project-statuses.json").write_text(
        json.dumps({"projects": {"New alias": {"status": "done", "summary": "Stale legacy status"}}}),
        encoding="utf-8",
    )
    client = TestClient(create_app(store, auth_token="atlas-test"))
    auth = {"Authorization": "Bearer atlas-test"}
    work = WorkService(store)
    item = next(item for item in work.list(active.id) if item.title == "Research status")
    work.check_in(active.id, item.id,
                  WorkCheckIn(summary="New live check-in", status="blocked", next_step="Resolve blocker"),
                  actor_kind="operator", actor_id="fixture")
    result = client.get("/api/v1/atlas", headers=auth).json()
    focused = next(project for project in result["grandThreatProjects"] if project["name"] == "focused_research")
    assert focused["workflow"]["status"] == "blocked"
    assert focused["workflow"]["summary"] == "New live check-in"
    assert focused["workflow"]["nextStep"] == "Resolve blocker"

    supporting = root / "projects" / "supporting_research"
    (supporting / "data" / "binaries" / "legacy.bin").unlink()
    for targets in ([], [{"analysisBinaryPath": "data/missing-binary"}]):
        (supporting / "project_metadata.json").write_text(
            json.dumps({"targets": targets}), encoding="utf-8"
        )
        refreshed = client.get("/api/v1/atlas", headers=auth)
        assert refreshed.status_code == 200
        project = next(project for project in refreshed.json()["grandThreatProjects"]
                       if project["name"] == "supporting_research")
        assert project["binaries"]["count"] is None
        assert project["binaries"]["totalBytes"] is None


def test_atlas_unavailable_without_linked_project_or_explicit_root(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_INTEL_ATLAS_ROOT", raising=False)
    store = NebulaStore(tmp_path / "core.db")
    client = TestClient(create_app(store, auth_token="atlas-test"))
    response = client.get("/api/v1/atlas", headers={"Authorization": "Bearer atlas-test"})
    assert response.status_code == 200
    assert response.json()["source"]["available"] is False
    assert response.json()["grandThreatProjects"] == []
