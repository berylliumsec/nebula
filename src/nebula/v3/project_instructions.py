"""Project-root AGENTS.md: re-read for every provider turn, never compacted.

AGENTS.md is the cross-tool convention for project instructions. Nebula places it
in the per-turn system instructions, which are rebuilt on every turn and are never
part of the transcript that context compaction summarizes.
"""

from __future__ import annotations

import hashlib
import codecs
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints

from .domain import NebulaModel

AGENTS_FILENAME = "AGENTS.md"
MAX_PROJECT_INSTRUCTIONS_BYTES = 64 * 1024


class ProjectInstructionsError(ValueError):
    pass


class ProjectInstructions(NebulaModel):
    path: str = Field(min_length=1, max_length=4_096)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    truncated: bool = False
    content: Annotated[str, StringConstraints(strip_whitespace=False)]

    def receipt(self) -> dict[str, object]:
        """Metadata recorded on the turn; the content itself lives in the model request."""

        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "truncated": self.truncated,
        }


def load_project_instructions(workspace: Path) -> ProjectInstructions | None:
    """Read `<workspace>/AGENTS.md` if present.

    A symlink is followed when it resolves inside the workspace or to an AGENTS.md
    in the workspace or one of its parent folders. Content beyond
    the size cap is cut at a UTF-8 boundary and marked as truncated.
    """

    root = workspace.resolve()
    candidate = root / AGENTS_FILENAME
    if not os.path.lexists(candidate):
        return None
    resolved = candidate.resolve()
    # Sub-projects commonly link to a parent program's AGENTS.md. Allow a link into
    # the workspace or to an AGENTS.md in the workspace or one of its ancestors, but
    # never to an arbitrary file elsewhere (which would expose it to the model).
    shared_parent = resolved.name == AGENTS_FILENAME and root.is_relative_to(
        resolved.parent
    )
    if not resolved.is_relative_to(root) and not shared_parent:
        raise ProjectInstructionsError(
            "AGENTS.md must stay inside the project workspace or link to an "
            "AGENTS.md in one of its parent folders"
        )
    if not resolved.is_file():
        raise ProjectInstructionsError("AGENTS.md must be a regular file")
    size = resolved.stat().st_size
    with resolved.open("rb") as handle:
        raw = handle.read(MAX_PROJECT_INSTRUCTIONS_BYTES + 1)
    truncated = len(raw) > MAX_PROJECT_INSTRUCTIONS_BYTES
    body = raw[:MAX_PROJECT_INSTRUCTIONS_BYTES] if truncated else raw
    # A non-final decode withholds only an incomplete trailing sequence. Keep
    # complete U+FFFD characters and the existing malformed-interior replacement
    # policy; trimming a decoded replacement character cannot distinguish them.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    content = decoder.decode(body, final=not truncated)
    return ProjectInstructions(
        path=AGENTS_FILENAME,
        # Hash exactly what the model receives.
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        size_bytes=size,
        truncated=truncated,
        content=content,
    )


def project_instructions_text(instructions: ProjectInstructions | None) -> str:
    if instructions is None or not instructions.content.strip():
        return ""
    note = (
        f" Only the first {MAX_PROJECT_INSTRUCTIONS_BYTES // 1024} KiB of "
        f"{instructions.size_bytes} bytes are included."
        if instructions.truncated
        else ""
    )
    # JSON keeps the file inside an explicit data value, like skill snapshots.
    return (
        f"\n\nProject instructions (AGENTS.md at the project root).{note}\n"
        + json.dumps(
            {"path": instructions.path, "content": instructions.content},
            ensure_ascii=False,
        )
    )
