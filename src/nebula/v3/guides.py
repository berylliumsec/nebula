"""In-app guide progress and the read-only probes guides use to confirm a step.

Progress lives in Core, keyed by the active operator profile, so a guide started on
the desktop can be resumed from a paired phone. Probes only report state that the
operator created themselves (files in the project workspace); they never change it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from .database import EntityRow
from .diagnostics import scrub_host_paths
from .domain import Engagement, GuideProgress, utc_now
from .operators import OperatorProfileService
from .project_instructions import (
    AGENTS_FILENAME,
    MAX_PROJECT_INSTRUCTIONS_BYTES,
    ProjectInstructionsError,
    load_project_instructions,
)
from .storage import ConflictError, NebulaStore, NotFoundError

# Guides still work before anyone creates an operator profile.
UNNAMED_OPERATOR = "local"
GUIDE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"


class GuideProgressWrite(BaseModel):
    status: Literal["in_progress", "completed", "dismissed"]
    step_index: int = Field(default=0, ge=0, le=64)
    expected_revision: int = Field(ge=0)


class StarterFilesRequest(BaseModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    kind: Literal["hook", "skill", "agents_md"]
    # Folder name for a hook or skill; ignored for AGENTS.md.
    name: str = Field(default="", max_length=63)


class StarterFilesResult(BaseModel):
    kind: str
    # Workspace-relative paths, in the order a guide should open them.
    paths: list[str]


class ProjectInstructionsStatus(BaseModel):
    filename: str = AGENTS_FILENAME
    present: bool
    size_bytes: int = 0
    truncated: bool = False
    limit_bytes: int = MAX_PROJECT_INSTRUCTIONS_BYTES
    error: str | None = None


def progress_id(operator_key: str, guide_id: str) -> str:
    return f"guide-progress:{operator_key}:{guide_id}"


def guides_router(
    store: NebulaStore,
    operators: OperatorProfileService,
    workspace_for: Callable[[str], Path],
) -> APIRouter:
    router = APIRouter(tags=["guides"])

    def operator_key() -> str:
        active = operators.active_profile_or_none()
        return active.id if active else UNNAMED_OPERATOR

    @router.get("/guides/progress", response_model=list[GuideProgress])
    def read_progress() -> list[GuideProgress]:
        with store.database.session() as database:
            rows = database.scalars(
                select(EntityRow)
                .where(
                    EntityRow.kind == GuideProgress.entity_kind,
                    EntityRow.payload["operator_key"].as_string() == operator_key(),
                )
                .order_by(EntityRow.created_at, EntityRow.id)
            )
            return [GuideProgress.model_validate(row.payload) for row in rows]

    @router.put("/guides/progress/{guide_id}", response_model=GuideProgress)
    def write_progress(guide_id: str, body: GuideProgressWrite) -> GuideProgress:
        if not _valid_guide_id(guide_id):
            raise HTTPException(422, "Unknown guide identity")
        key = operator_key()
        identity = progress_id(key, guide_id)
        completed_at = utc_now() if body.status == "completed" else None
        if body.expected_revision == 0:
            return store.create(
                GuideProgress(
                    id=identity,
                    operator_key=key,
                    guide_id=guide_id,
                    status=body.status,
                    step_index=body.step_index,
                    completed_at=completed_at,
                )
            )
        current = store.get(GuideProgress, identity)
        return store.update(
            GuideProgress,
            identity,
            {
                "status": body.status,
                "step_index": body.step_index,
                # Keep the first completion time when a finished guide is replayed.
                "completed_at": (
                    (current.completed_at or completed_at)
                    if body.status == "completed"
                    else None
                ),
            },
            expected_revision=body.expected_revision,
        )

    @router.delete("/guides/progress/{guide_id}", status_code=204)
    def reset_progress(guide_id: str) -> Response:
        if not _valid_guide_id(guide_id):
            raise HTTPException(422, "Unknown guide identity")
        try:
            store.delete(GuideProgress, progress_id(operator_key(), guide_id))
        except (
            NotFoundError
        ):  # diagnostic-expected: resetting an unstarted guide is a no-op
            pass
        return Response(status_code=204)

    @router.get(
        "/project-instructions",
        response_model=ProjectInstructionsStatus,
    )
    def project_instructions_status(engagement_id: str) -> ProjectInstructionsStatus:
        store.get(Engagement, engagement_id)
        try:
            instructions = load_project_instructions(workspace_for(engagement_id))
        except (
            ProjectInstructionsError,
            OSError,
            ValueError,
        ) as exc:  # diagnostic-expected: the refusal reason is returned to the guide step
            return ProjectInstructionsStatus(
                present=True, error=scrub_host_paths(str(exc))
            )
        if instructions is None:
            return ProjectInstructionsStatus(present=False)
        return ProjectInstructionsStatus(
            present=True,
            size_bytes=instructions.size_bytes,
            truncated=instructions.truncated,
        )

    @router.post(
        "/guides/starter-files", response_model=StarterFilesResult, status_code=201
    )
    def create_starter_files(body: StarterFilesRequest) -> StarterFilesResult:
        store.get(Engagement, body.engagement_id)
        workspace = workspace_for(body.engagement_id).resolve()
        files = starter_files(body.kind, body.name)
        write_new_files(workspace, files)
        return StarterFilesResult(kind=body.kind, paths=[path for path, _, _ in files])

    return router


STARTER_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

HOOK_SCRIPT = """#!/bin/sh
# Nebula sends one JSON event on stdin, e.g.
# {"schema":"nebula.native-hook-event/v1","event":"chat.turn.started",...}
# This starter appends each event to events.jsonl beside this script.
# Replace it with your own logic. Exit non-zero to report a failure.
cat >> events.jsonl
echo >> events.jsonl
"""

AGENTS_TEMPLATE = """# Project instructions

Nebula re-reads this file on every assistant turn and never compacts it.
Keep it short: the rules that must always hold for this project.

## Scope
- In scope:
- Out of scope:

## Rules of engagement
- 
"""

SKILL_CHECKLIST = """# Checklist

Replace these with the checks the skill must complete, one per line.

- [ ] Confirm the target is in scope before touching it.
- [ ] Record evidence for every finding.
- [ ] Summarise what was verified and what was not.
"""


def starter_files(kind: str, name: str) -> list[tuple[str, str, int]]:
    """Return (relative path, content, mode) for a guide's starter files."""

    if kind == "agents_md":
        return [(AGENTS_FILENAME, AGENTS_TEMPLATE, 0o644)]
    if re.fullmatch(STARTER_NAME_PATTERN, name) is None:
        raise ValueError(
            "Use lowercase letters, digits and dashes for the name, e.g. audit"
        )
    if kind == "hook":
        manifest = {
            "version": 1,
            "name": name.replace("-", " ").capitalize(),
            "description": "Starter hook created by the Nebula guide.",
            "events": ["chat.turn.started", "chat.turn.completed"],
            "command": ["run.sh"],
            "timeout_seconds": 10,
            "side_effects": "workspace",
            "failure_policy": "continue",
        }
        folder = f".agents/hooks/{name}"
        return [
            (f"{folder}/hook.json", json.dumps(manifest, indent=2) + "\n", 0o644),
            (f"{folder}/run.sh", HOOK_SCRIPT, 0o755),
        ]
    # Every relative link in SKILL.md must resolve when the skill is selected,
    # so the example link points at a file the starter writes too.
    skill = (
        "---\n"
        f"name: {name}\n"
        "description: Say when the assistant should use this skill.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Write the steps the assistant should follow. Link supporting files in this\n"
        "folder with relative links, e.g. [checklist](checklist.md).\n"
    )
    folder = f".agents/skills/{name}"
    return [
        (f"{folder}/SKILL.md", skill, 0o644),
        (f"{folder}/checklist.md", SKILL_CHECKLIST, 0o644),
    ]


def write_new_files(workspace: Path, files: list[tuple[str, str, int]]) -> None:
    """Create files without following symlinks or replacing anything that exists."""

    for relative, _, _ in files:
        if (workspace / relative).exists() or (workspace / relative).is_symlink():
            raise ConflictError(f"{relative} already exists; open it instead")
    for relative, content, mode in files:
        target = workspace
        for part in Path(relative).parts[:-1]:
            target = target / part
            if target.is_symlink():
                raise ConflictError(f"{target.relative_to(workspace)} is a symlink")
            target.mkdir(mode=0o755, exist_ok=True)
        descriptor = os.open(
            target / Path(relative).name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        # The umask can strip the executable bit a hook needs.
        os.chmod(target / Path(relative).name, mode)


def _valid_guide_id(guide_id: str) -> bool:
    return re.fullmatch(GUIDE_ID_PATTERN, guide_id) is not None


__all__ = [
    "GuideProgressWrite",
    "ProjectInstructionsStatus",
    "StarterFilesRequest",
    "StarterFilesResult",
    "guides_router",
    "progress_id",
]
