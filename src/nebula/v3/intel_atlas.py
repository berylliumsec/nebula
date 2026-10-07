"""Bounded, read-only projection of a linked Apple research repository.

The threat map and project files are dated evidence. Current project status comes
only from Nebula Work. No legacy dashboard status store is consulted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha1
import json
import os
from pathlib import Path
import re
from stat import S_ISREG
import time
from typing import Any

from .domain import Engagement, EngagementStatus, WorkItem, WorkUpdate
from .storage import NebulaStore


MAP_NAME = "GRAND_THREAT_PROJECT_MAP.md"
DOCUMENT_NAMES = {
    "binary_functionality_map.md": "functionality",
    "functionality_map.md": "functionality",
    "attacker_input_inventory.md": "attacker",
}
FEATURED_DOCUMENTS = {
    MAP_NAME: "threat",
    "GRAND_THREAT_INTEL.md": "threat",
    "REMOTE_MAC_DATA_INGRESS_MAP.md": "threat",
    "KERNEL_DIRECT_EXTERNAL_INPUT_PROJECT_MAP.md": "kernel",
}
SKIP_DIRS = {".git", "node_modules", "scratch_space", "simulation_results", "runtime_artifacts", ".venv"}
OPEN_LANE_STATUSES = {"open", "needs_simulation", "ready_for_runtime", "stale"}
MAX_MAP_BYTES = 2_000_000
MAX_DOCUMENTS = 1_500
MAX_DIRS = 12_000
MAX_DOCUMENT_BYTES = 256_000
MAX_LANE_BYTES = 8_000_000
MAX_SIM_TAIL_BYTES = 2_000_000


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _read_text(path: Path, limit: int) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _json(path: Path, limit: int = 2_000_000) -> Any:
    raw = _read_text(path, limit)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _within(project: Path, path: Path) -> bool:
    return project.resolve() in path.resolve().parents


def _engagements(store: NebulaStore) -> list[Engagement]:
    return store.find_entities(Engagement, {}, limit=None)


def _repository_root(engagements: list[Engagement]) -> Path | None:
    def admissible(candidate: Path) -> bool:
        map_path = candidate / MAP_NAME
        return map_path.is_file() and candidate in map_path.resolve().parents

    configured = os.environ.get("NEBULA_INTEL_ATLAS_ROOT")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return candidate if admissible(candidate) else None
    candidates: set[Path] = set()
    for engagement in engagements:
        if not engagement.workspace_path:
            continue
        workspace = Path(engagement.workspace_path).expanduser().resolve()
        if workspace.parent.name != "projects":
            continue
        candidate = workspace.parent.parent
        if admissible(candidate):
            candidates.add(candidate)
    return sorted(candidates, key=str)[0] if candidates else None


def _memberships(markdown: str) -> dict[str, dict[str, set[str]]]:
    membership: dict[str, dict[str, set[str]]] = {}
    table = markdown.split("## Surface-to-project index", 1)
    section = table[1].split("\n## ", 1)[0] if len(table) == 2 else ""
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("| Grand threat surface"):
            continue
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) < 3:
            continue
        for cell, role in ((cells[1], "focused"), (cells[2], "supporting")):
            for project_path in re.findall(r"\]\((projects/[^)#]+)\)", cell):
                entry = membership.setdefault(project_path.rstrip("/"), {"surfaces": set(), "roles": set()})
                entry["surfaces"].add(cells[0])
                entry["roles"].add(role)
    stage = "Additional map anchor"
    for line in markdown.splitlines():
        if line.startswith("### "):
            stage = line[4:]
        for project_path in re.findall(r"\]\((projects/[^)#]+)\)", line):
            membership.setdefault(project_path.rstrip("/"), {"surfaces": {stage}, "roles": {"stage anchor"}})
    return membership


def _lanes(project: Path) -> dict[str, Any]:
    unknown = {"indexed": False, "total": None, "open": None, "needsSimulation": None,
               "readyForRuntime": None, "stale": None}
    lane_path = project / "artifacts/knowledge/research_lanes.jsonl"
    raw = _read_text(lane_path, MAX_LANE_BYTES) if _within(project, lane_path) else None
    if raw is None:
        return unknown
    rows = []
    try:
        for line in raw.splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except ValueError:
        return unknown
    statuses = []
    for row in rows:
        status = row.get("status") if isinstance(row, dict) else None
        if (status == "bounded" and row.get("symexec_required") is True
                and row.get("symexec_requirement_source") in {"lane_regression", "memop_regression"}
                and row.get("symexec_validated") is not True):
            status = "needs_simulation"
        statuses.append(status)
    return {"indexed": True, "total": len(rows),
            "open": sum(status in OPEN_LANE_STATUSES for status in statuses),
            "needsSimulation": statuses.count("needs_simulation"),
            "readyForRuntime": statuses.count("ready_for_runtime"),
            "stale": statuses.count("stale")}


def _v2_migration(project: Path, audit: dict[str, Any] | None, audited_at: str | None) -> dict[str, Any]:
    evidence = project / "artifacts/evidence"
    status, label, detail = "not_migrated", "Not migrated", "No v2 graph or sidecar."
    manifest_path = evidence / "graph_manifest.json"
    if manifest_path.exists() and _within(project, manifest_path):
        manifest = _json(manifest_path)
        if isinstance(manifest, dict) and manifest.get("schema_version") == "2.0" and manifest.get("mode") in {"canonical", "shadow"}:
            nodes = evidence / "investigation_nodes.jsonl"
            edges = evidence / "investigation_edges.jsonl"
            if (not _within(project, nodes) or not _within(project, edges)
                    or not nodes.is_file() or nodes.stat().st_size == 0 or not edges.is_file()):
                status, label, detail = "incomplete", "V2 graph incomplete", "Typed graph files are missing."
            else:
                mode = manifest["mode"]
                status, label = f"{mode}_v2", "Canonical v2" if mode == "canonical" else "Shadow v2"
                detail = "Canonical cutover recorded." if mode == "canonical" else "Canonical cutover incomplete."
        else:
            status, label, detail = "unknown", "Unknown migration state", "Graph manifest unreadable or unsupported."
    else:
        sidecar_path = evidence / "unresolved_import/manifest.json"
        if sidecar_path.exists() and _within(project, sidecar_path):
            sidecar = _json(sidecar_path)
            if isinstance(sidecar, dict) and sidecar.get("mode") == "unresolved_import":
                status, label, detail = "provenance_only", "Provenance only", "Sidecar present; canonical migration incomplete."
            else:
                status, label, detail = "unknown", "Unknown migration state", "Sidecar unreadable or unsupported."
    audit = audit if isinstance(audit, dict) else None
    return {"status": status, "label": label, "detail": detail,
            "validation": audit.get("validation", "not_audited") if audit else "not_audited",
            "auditedAt": audited_at if audit else None,
            "auditDetail": audit.get("detail", "No recorded validation audit.") if audit else "No recorded validation audit.",
            "remainingGate": audit.get("remainingGate") if audit else None}


def _binaries(root: Path, project: Path) -> dict[str, Any]:
    unknown = {"count": None, "totalBytes": None, "files": []}
    metadata_path = project / "project_metadata.json"
    if not _within(project, metadata_path):
        return unknown
    metadata = _json(metadata_path, 512_000)
    targets = metadata.get("targets") if isinstance(metadata, dict) else None
    if targets is not None and not isinstance(targets, list):
        return unknown
    files = []
    seen: set[Path] = set()
    if isinstance(targets, list) and len(targets) > 100:
        return unknown
    for target in (targets or []):
        if not isinstance(target, dict):
            continue
        for key in ("analysisBinaryPath", "sourceBinaryPath"):
            value = target.get(key)
            if not isinstance(value, str) or not value:
                continue
            path = (root / value).resolve()
            if root not in path.parents or path in seen:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if not path.is_file():
                continue
            seen.add(path)
            files.append({"name": path.name, "path": path.relative_to(root).as_posix(), "size": stat.st_size})
    if targets:
        return ({"count": len(files), "totalBytes": sum(item["size"] for item in files), "files": files}
                if files else unknown)
    # Older projects can hold local binary copies without project_metadata.json.
    local = project / "data/binaries"
    if not local.is_dir() or not _within(project, local):
        return unknown
    visited = 0
    for folder, dirs, names in os.walk(local, followlinks=False):
        visited += 1
        if visited > 500 or len(files) + len(names) > 100:
            return unknown
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in names:
            path = Path(folder) / name
            if not _within(project, path) or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append({"name": name, "path": path.relative_to(root).as_posix(), "size": size})
    return ({"count": len(files), "totalBytes": sum(item["size"] for item in files), "files": files}
            if files else unknown)


def _document(root: Path, path: Path, kind: str, featured: bool = False) -> dict[str, Any] | None:
    if root not in path.resolve().parents:
        return None
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES).decode("utf-8", errors="replace")
        modified = path.stat().st_mtime
    except OSError:
        return None
    relative = path.relative_to(root).as_posix()
    headings = [heading.replace("`", "") for heading in re.findall(r"^#{2,4}\s+(.+)$", raw, re.M)[:14]]
    title_match = re.search(r"^#\s+(.+)$", raw, re.M)
    title = title_match.group(1).replace("`", "") if title_match else path.stem
    excerpt = re.sub(r"\s+", " ", re.sub(r"[#|`*_>~-]", " ", re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", re.sub(r"```[\s\S]*?```", " ", raw)))).strip()[:310]
    return {"id": sha1(relative.encode()).hexdigest()[:12], "kind": kind, "featured": featured,
            "project": relative.split("/")[1] if relative.startswith("projects/") else "cross-project",
            "path": relative, "title": title, "excerpt": excerpt, "headings": headings,
            "updatedAt": _iso(datetime.fromtimestamp(modified, timezone.utc))}


@lru_cache(maxsize=8)
def _documents_cached(root: Path, thirty_second_bucket: int) -> tuple[dict[str, Any], ...]:
    records = []
    for name, kind in FEATURED_DOCUMENTS.items():
        record = _document(root, root / name, kind, True)
        if record:
            records.append(record)
    visited = 0
    for tree in (root / "projects", root / "data"):
        if not tree.is_dir():
            continue
        for folder, dirs, files in os.walk(tree, followlinks=False):
            visited += 1
            if visited > MAX_DIRS or len(records) >= MAX_DOCUMENTS:
                break
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
            for name in files:
                kind = DOCUMENT_NAMES.get(name)
                if kind:
                    record = _document(root, Path(folder) / name, kind)
                    if record:
                        records.append(record)
                    if len(records) >= MAX_DOCUMENTS:
                        break
    records.sort(key=lambda record: (not record["featured"], record["title"].casefold()))
    return tuple(records)


def _simulations(root: Path, projects: list[Path]) -> list[dict[str, Any]]:
    candidates = []
    project_set = set(projects)
    try:
        with os.scandir(root / "projects") as entries:
            for entry in entries:
                if len(project_set) >= 5_000:
                    break
                if entry.is_dir(follow_symlinks=False):
                    project_set.add(Path(entry.path))
    except OSError:
        pass
    for project in project_set:
        path = project / "artifacts/knowledge/simulation_runs.jsonl"
        if not _within(project, path):
            continue
        try:
            stat = path.stat()
            if S_ISREG(stat.st_mode) and stat.st_size:
                candidates.append((stat.st_mtime, path, project.name))
        except OSError:
            pass
    records = []
    for mtime, path, project_name in sorted(candidates, reverse=True)[:80]:
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - MAX_SIM_TAIL_BYTES))
                raw = stream.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            if path.stat().st_size > MAX_SIM_TAIL_BYTES:
                lines = lines[1:]
            for line in lines[-5:]:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                records.append({"project": project_name, "runKey": str(row.get("run_key") or "unknown-run"),
                                "laneKey": row.get("lane_key"), "hypothesisKey": row.get("hypothesis_key"),
                                "status": str(row.get("status") or "unknown"),
                                "feasibility": str(row.get("feasibility") or "unknown"),
                                "decisionImpact": str(row.get("decision_impact") or "unknown"),
                                "summary": str(row.get("summary") or "")[:1200],
                                "updatedAt": _iso(datetime.fromtimestamp(mtime, timezone.utc))})
        except OSError:
            continue
    return sorted(records, key=lambda row: row["updatedAt"] or "", reverse=True)[:250]


def atlas_index(store: NebulaStore) -> dict[str, Any]:
    engagements = _engagements(store)
    root = _repository_root(engagements)
    generated_at = _iso(datetime.now(timezone.utc))
    source = {"available": root is not None, "mapPath": MAP_NAME, "mapSnapshot": None,
              "statusAuthority": "nebula-work", "migrationAuthority": "project-evidence-and-dated-audit"}
    empty = {"generatedAt": generated_at, "source": source,
             "counts": {"grandThreatProjects": 0, "openLanes": None, "needsSimulation": None,
                        "documents": 0, "simulations": 0},
             "grandThreatProjects": [], "documents": [], "simulations": []}
    if root is None:
        return empty
    markdown = _read_text(root / MAP_NAME, MAX_MAP_BYTES)
    if markdown is None:
        source["available"] = False
        return empty
    snapshot = re.search(r"^Snapshot:\s*(.+)$", markdown, re.M)
    source["mapSnapshot"] = snapshot.group(1).strip() if snapshot else None
    membership = _memberships(markdown)
    audits = _json(root / "dashboard/data/migration-statuses.json")
    audit_projects = audits.get("projects", {}) if isinstance(audits, dict) else {}
    audited_at = audits.get("auditedAt") if isinstance(audits, dict) else None
    linked: dict[Path, Engagement] = {}
    for engagement in engagements:
        if not engagement.workspace_path:
            continue
        workspace = Path(engagement.workspace_path).expanduser().resolve()
        if workspace != root and root not in workspace.parents:
            continue
        existing = linked.get(workspace)
        # Prefer the active linked project; a later update wins within that class.
        rank = (engagement.status == EngagementStatus.ACTIVE, engagement.status != EngagementStatus.ARCHIVED,
                engagement.updated_at, engagement.id)
        if existing is None or rank > (existing.status == EngagementStatus.ACTIVE,
                                      existing.status != EngagementStatus.ARCHIVED,
                                      existing.updated_at, existing.id):
            linked[workspace] = engagement
    projects = []
    project_roots = []
    all_lanes_known = True
    open_lanes = needs_simulation = 0
    for relative, entry in sorted(membership.items()):
        if not re.fullmatch(r"projects/(?:[^/]+/)*[^/]+", relative) or ".." in Path(relative).parts:
            continue
        project = root / relative
        if not project.is_dir() or project.is_symlink() or root not in project.resolve().parents:
            missing = True
            engagement = None
        else:
            missing = False
            project_roots.append(project)
            engagement = linked.get(project.resolve())
        work_item = None
        update = None
        if engagement and engagement.work_enabled:
            items = store.find_entities(WorkItem, {"title": "Research status"},
                                        engagement_id=engagement.id, limit=1, newest_first=True)
            work_item = items[0] if items else None
            if work_item:
                updates = store.find_entities(WorkUpdate, {"item_id": work_item.id},
                                              engagement_id=engagement.id, limit=1, newest_first=True)
                update = updates[0] if updates else None
        workflow = {"status": work_item.status if work_item else "unknown",
                    "summary": update.summary if update else "No current Research status check-in.",
                    "nextStep": update.next_step if update else None,
                    "updatedAt": _iso(update.created_at if update else work_item.last_update_at if work_item else None),
                    "source": "nebula-work" if work_item else "unknown"}
        lanes = _lanes(project) if not missing else {"indexed": False, "total": None, "open": None,
                                                     "needsSimulation": None, "readyForRuntime": None,
                                                     "stale": None}
        if not lanes["indexed"]:
            all_lanes_known = False
        else:
            open_lanes += lanes["open"]
            needs_simulation += lanes["needsSimulation"]
        audit = audit_projects.get(relative) if isinstance(audit_projects, dict) else None
        migration_status = "missing" if missing else "linked" if engagement else "unlinked"
        project_record = {"name": project.name, "path": relative, "missing": missing,
                          "surfaces": sorted(entry["surfaces"]), "roles": sorted(entry["roles"]),
                          "lastWorkedAt": workflow["updatedAt"],
                          "workspace": {"linked": engagement is not None, "engagementId": engagement.id if engagement else None},
                          "workflow": workflow,
                          "migration": {"status": migration_status,
                                        "label": {"missing": "Missing project", "linked": "Linked workspace", "unlinked": "Unlinked workspace"}[migration_status],
                                        "detail": "Current Nebula workspace link." if engagement else "No current Nebula workspace link."},
                          "v2Migration": _v2_migration(project, audit, audited_at) if not missing else
                                         {"status": "unknown", "label": "Missing project", "detail": "Project root missing.",
                                          "validation": audit.get("validation", "not_audited") if isinstance(audit, dict) else "not_audited",
                                          "auditedAt": audited_at if isinstance(audit, dict) else None,
                                          "auditDetail": audit.get("detail", "No recorded validation audit.") if isinstance(audit, dict) else "No recorded validation audit.",
                                          "remainingGate": audit.get("remainingGate") if isinstance(audit, dict) else None},
                          "binaries": _binaries(root, project) if not missing else
                                      {"count": None, "totalBytes": None, "files": []},
                          "lanes": lanes}
        projects.append(project_record)
    documents = list(_documents_cached(root, int(time.monotonic() // 30)))
    simulations = _simulations(root, project_roots)
    empty.update({"counts": {"grandThreatProjects": len(projects),
                              "openLanes": open_lanes if all_lanes_known else None,
                              "needsSimulation": needs_simulation if all_lanes_known else None,
                              "documents": len(documents), "simulations": len(simulations)},
                  "grandThreatProjects": projects, "documents": documents, "simulations": simulations})
    return empty


def atlas_document(store: NebulaStore, document_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{12}", document_id):
        return None
    index = atlas_index(store)
    root = _repository_root(_engagements(store))
    if root is None:
        return None
    record = next((item for item in index["documents"] if item["id"] == document_id), None)
    if record is None:
        return None
    path = (root / record["path"]).resolve()
    if root not in path.parents or not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError:
        return None
    return {**record, "content": payload[:MAX_DOCUMENT_BYTES].decode("utf-8", errors="replace"),
            "truncated": len(payload) > MAX_DOCUMENT_BYTES}
