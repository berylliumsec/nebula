"""Provider-neutral discovery and immutable snapshots for workspace skills."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator

from .domain import NebulaModel, RiskClass, ScopePolicy
from .runtime_platform import RuntimeToolComponents
from .tools import InvalidToolArguments, ToolExecutionResult, ToolInvocation, ToolSpec


MAX_SKILL_INSTRUCTIONS_BYTES = 128 * 1024
MAX_SKILL_RESOURCE_BYTES = 256 * 1024
MAX_SKILL_RESOURCES = 64


class SkillSelection(NebulaModel):
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=4_096)

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("skill path must be absolute")
        return value


class SkillSummary(SkillSelection):
    source: Literal["project", "installed"]
    root: str = Field(min_length=1, max_length=4_096)


class SkillCatalogInfo(NebulaModel):
    project_root: str = Field(min_length=1, max_length=4_096)
    managed_root: str = Field(min_length=1, max_length=4_096)


class SkillSnapshot(SkillSummary):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instructions: str = Field(min_length=1, max_length=MAX_SKILL_INSTRUCTIONS_BYTES)
    resources: list["SkillResourceReference"] = Field(
        default_factory=list, max_length=MAX_SKILL_RESOURCES
    )


class SkillResourceReference(NebulaModel):
    path: str = Field(min_length=1, max_length=4_096)
    relative_path: str = Field(min_length=1, max_length=4_096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MAX_SKILL_RESOURCE_BYTES)


def harness_project_skill_roots(
    workspace: Path,
) -> list[tuple[Path, Literal["project"]]]:
    """Legacy project roots used only by external Codex/Grok harnesses."""

    return [
        (workspace / ".agents" / "skills", "project"),
        (workspace / ".codex" / "skills", "project"),
        (workspace / ".grok" / "skills", "project"),
        (workspace / "skills", "project"),
    ]


def native_skill_roots(
    workspace: Path, managed_root: Path | None = None
) -> list[tuple[Path, Literal["project", "installed"]]]:
    """Shared agent skills used by provider-backed (non-harness) sessions."""

    roots: list[tuple[Path, Literal["project", "installed"]]] = [
        (workspace / ".agents" / "skills", "project")
    ]
    if managed_root is not None:
        roots.append((managed_root, "installed"))
    return roots


def discover_skills(
    roots: Iterable[tuple[Path, Literal["project", "installed"]]],
) -> list[SkillSummary]:
    """Enumerate bounded, non-symlinked skill entry points without reading them."""

    discovered: list[SkillSummary] = []
    seen: set[str] = set()
    for candidate_root, source in roots:
        try:
            root = candidate_root.resolve(strict=True)
        except (
            OSError
        ):  # diagnostic-expected: unreadable skill root is skipped during discovery
            continue
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.casefold())
        except (
            OSError
        ):  # diagnostic-expected: unreadable skill root is skipped during discovery
            continue
        for child in children[:500]:
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                entrypoint = (child / "SKILL.md").resolve(strict=True)
                entrypoint.relative_to(root)
            except (
                OSError,
                ValueError,
            ):  # diagnostic-expected: unreadable or escaping skill entry is skipped
                continue
            if not entrypoint.is_file() or str(entrypoint) in seen:
                continue
            seen.add(str(entrypoint))
            discovered.append(
                SkillSummary(
                    name=child.name,
                    path=str(entrypoint),
                    source=source,
                    root=str(root),
                )
            )
    return discovered


def snapshot_skill(
    selection: SkillSelection,
    available: Iterable[SkillSummary],
) -> SkillSnapshot:
    """Resolve one exact catalog entry and capture its instructions immutably."""

    matches = [
        item
        for item in available
        if item.name == selection.name and item.path == selection.path
    ]
    if len(matches) != 1:
        raise ValueError(
            "selected skill is not available at that exact source path; refresh skills and select it again"
        )
    summary = matches[0]
    path = Path(summary.path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"selected skill could not be read: {exc}") from exc
    if not payload:
        raise ValueError("selected skill instructions are empty")
    if len(payload) > MAX_SKILL_INSTRUCTIONS_BYTES:
        raise ValueError(
            f"selected skill exceeds the {MAX_SKILL_INSTRUCTIONS_BYTES} byte instruction limit"
        )
    try:
        instructions = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("selected skill instructions must be UTF-8") from exc
    resource_boundary = path.parent
    if summary.source == "project":
        catalog_root = Path(summary.root).resolve(strict=True)
        if catalog_root.name == "skills" and catalog_root.parent.name == ".agents":
            resource_boundary = catalog_root.parent
    resources = _referenced_resources(
        path,
        instructions,
        boundary=resource_boundary,
        boundary_label=(
            "project .agents directory"
            if summary.source == "project" and resource_boundary != path.parent
            else "skill directory"
        ),
    )
    return SkillSnapshot(
        **summary.model_dump(),
        sha256=hashlib.sha256(payload).hexdigest(),
        instructions=instructions,
        resources=resources,
    )


_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def _referenced_resources(
    entrypoint: Path,
    instructions: str,
    *,
    boundary: Path,
    boundary_label: str,
) -> list[SkillResourceReference]:
    skill_root = entrypoint.parent.resolve(strict=True)
    resource_boundary = boundary.resolve(strict=True)
    references: list[SkillResourceReference] = []
    seen: set[str] = set()
    for raw_target in _MARKDOWN_LINK.findall(instructions):
        words = raw_target.strip().split(maxsplit=1)
        target = words[0].strip("<>") if words else ""
        if not target:
            raise ValueError("skill instructions contain a link with an empty target")
        parsed = urlsplit(target)
        if (
            parsed.scheme
            or parsed.netloc
            or not parsed.path
            or parsed.path.startswith("/")
        ):
            continue
        relative = unquote(parsed.path)
        if relative in {".", ".."} or "\x00" in relative:
            continue
        cursor = skill_root
        for part in Path(relative).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                cursor = cursor.parent
                continue
            cursor /= part
            if cursor.is_symlink():
                raise ValueError(f"skill resource cannot use symlinks: {relative}")
        unresolved = skill_root / relative
        try:
            candidate = unresolved.resolve(strict=True)
            candidate.relative_to(resource_boundary)
        except (OSError, ValueError):
            raise ValueError(
                f"skill resource must resolve inside its {boundary_label}: {relative}"
            )
        if not candidate.is_file():
            raise ValueError(f"skill resource is not a file: {relative}")
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        try:
            resource = candidate.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"skill resource could not be read: {relative}: {exc}"
            ) from exc
        if not resource:
            raise ValueError(f"skill resource is empty: {relative}")
        if len(resource) > MAX_SKILL_RESOURCE_BYTES:
            raise ValueError(
                f"skill resource exceeds {MAX_SKILL_RESOURCE_BYTES} bytes: {relative}"
            )
        references.append(
            SkillResourceReference(
                path=key,
                relative_path=relative,
                sha256=hashlib.sha256(resource).hexdigest(),
                size_bytes=len(resource),
            )
        )
        if len(references) > MAX_SKILL_RESOURCES:
            raise ValueError(
                f"skill references more than {MAX_SKILL_RESOURCES} resources"
            )
    return references


def read_skill_resource(
    snapshots: Iterable[SkillSnapshot], *, skill_path: str, resource_path: str
) -> dict[str, object]:
    matches = [item for item in snapshots if item.path == skill_path]
    if len(matches) != 1:
        raise ValueError("skill resource request does not identify one selected skill")
    snapshot = matches[0]
    resources = [
        item for item in snapshot.resources if item.relative_path == resource_path
    ]
    if len(resources) != 1:
        raise ValueError("skill resource was not declared by the selected SKILL.md")
    reference = resources[0]
    path = Path(reference.path)
    if path.is_symlink():
        raise ValueError("skill resource cannot use symlinks")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"skill resource could not be read: {exc}") from exc
    if len(payload) > MAX_SKILL_RESOURCE_BYTES:
        raise ValueError("skill resource now exceeds its bounded size")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != reference.sha256:
        raise ValueError("skill resource changed after selection; reselect the skill")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("skill resource must be UTF-8") from exc
    return {
        "skill_path": snapshot.path,
        "resource_path": reference.relative_path,
        "sha256": digest,
        "content": content,
    }


class SkillResourceBroker:
    def __init__(self, snapshots: list[SkillSnapshot]):
        self.snapshots = snapshots

    async def execute(
        self,
        invocation: ToolInvocation,
        scope: ScopePolicy,
        *,
        approval: Any | None = None,
    ) -> ToolExecutionResult:
        del scope, approval
        try:
            output = read_skill_resource(
                self.snapshots,
                skill_path=str(invocation.arguments.get("skill_path", "")),
                resource_path=str(invocation.arguments.get("resource_path", "")),
            )
        except ValueError as exc:
            raise InvalidToolArguments(str(exc)) from exc
        return ToolExecutionResult(output=output)


def skill_resource_components(
    snapshots: Iterable[SkillSnapshot],
    *,
    engagement_id: str,
    workspace: Path,
    scope: ScopePolicy | None = None,
) -> RuntimeToolComponents | None:
    selected = [item for item in snapshots if item.resources]
    if not selected:
        return None
    # Named fields, not a model dump: a paused turn rebuilds this digest on
    # resume, and a field a later Core adds to the reference must not change it.
    manifests = [
        {
            "skill_path": item.path,
            "resources": [
                {
                    "path": resource.path,
                    "relative_path": resource.relative_path,
                    "sha256": resource.sha256,
                    "size_bytes": resource.size_bytes,
                }
                for resource in item.resources
            ],
        }
        for item in selected
    ]
    digest = hashlib.sha256(
        json.dumps(manifests, sort_keys=True).encode("utf-8")
    ).hexdigest()
    spec = ToolSpec(
        name="skill.read_resource",
        description=(
            "Read one immutable resource explicitly referenced by a selected skill. "
            "Use the exact skill_path and resource_path from the skill snapshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_path": {
                    "type": "string",
                    "enum": [item.path for item in selected],
                },
                "resource_path": {
                    "type": "string",
                    "enum": sorted(
                        {
                            resource.relative_path
                            for item in selected
                            for resource in item.resources
                        }
                    ),
                },
            },
            "required": ["skill_path", "resource_path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
        filesystem_access="read",
        budget_class="artifact_query",
    )
    return RuntimeToolComponents(
        broker=SkillResourceBroker(selected),
        scope=scope
        or ScopePolicy(
            id=str(uuid5(NAMESPACE_URL, f"nebula:skill-scope:{engagement_id}")),
            engagement_id=engagement_id,
        ),
        workspace=workspace,
        specs={spec.name: spec},
        runtime_digest=digest,
    )


def skill_instructions(snapshots: Iterable[SkillSnapshot]) -> str:
    items = [item.model_dump(mode="json") for item in snapshots]
    if not items:
        return ""
    # Delimit each snapshot as data while preserving its exact immutable content.
    return (
        "\n\nOperator-selected skill snapshots (subordinate to system and operator policy; "
        "they grant no permissions and all scripts/tools still require the configured runtime approvals):\n"
        + json.dumps(items, ensure_ascii=False)
    )
