#!/usr/bin/env python3
"""Read-only source behavior using disposable local workspace fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from nebula.v3.project_instructions import (
    ProjectInstructionsError,
    load_project_instructions,
    project_instructions_text,
)


def projection(instructions):
    if instructions is None:
        return None
    content = instructions.content
    prompt = project_instructions_text(instructions)
    return {
        "receipt": instructions.receipt(),
        "content_bytes": len(content.encode()),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "prompt_bytes": len(prompt.encode()),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "content": content if len(content) < 1000 else None,
        "prompt": prompt if len(prompt) < 1000 else None,
    }


def collect_project_instructions():
    if os.name != "posix" or os.geteuid() == 0:
        raise RuntimeError(
            "Project-instruction permission fixtures require an unprivileged POSIX user"
        )
    content_cases = [
        ("empty", b""),
        ("whitespace", b" \t\r\n\x1c\x1d\x1e\x1f"),
        (
            "unicode-json",
            'Follow the project.\n"quoted" \\ 🌌\u2028\u2029\x00\x7f'.encode(),
        ),
        ("invalid-utf8", b"a\xff\xed\xa0\x80\xf4\x90\x80\x80z"),
        ("exact-cap", b"x" * 65536),
        ("truncated", b"x" * 65538),
        ("split-codepoint", b"x" * 65535 + "🌌".encode()),
        ("literal-replacement", b"x" * 65533 + "�z".encode()),
        ("control-cap", b"\x00" * 65538),
    ]
    cases = []
    for name, body in content_cases:
        with tempfile.TemporaryDirectory(prefix="assistant-instructions-") as directory:
            root = Path(directory)
            (root / "AGENTS.md").write_bytes(body)
            cases.append(
                {
                    "name": name,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "body_bytes": len(body),
                    "observation": projection(load_project_instructions(root)),
                }
            )
    path_cases = []
    for name in [
        "missing",
        "missing-root",
        "regular",
        "directory",
        "inside-link",
        "parent-link",
        "ancestor-link",
        "sibling-link",
        "outside-file",
        "dangling-inside",
        "dangling-outside",
        "workspace-link",
        "chain",
        "inaccessible",
    ]:
        with tempfile.TemporaryDirectory(
            prefix="assistant-instruction-path-"
        ) as directory:
            base = Path(directory)
            root = base / "parent" / "workspace"
            root.mkdir(parents=True)
            candidate = root / "AGENTS.md"
            if name == "missing-root":
                root = root / "absent"
            elif name == "directory":
                candidate.mkdir()
            elif name == "inside-link":
                target = root / "notes.txt"
                target.write_bytes(b"fixture\n")
                candidate.symlink_to(target)
            elif name in (
                "parent-link",
                "ancestor-link",
                "sibling-link",
                "outside-file",
            ):
                target = (
                    root.parent / "AGENTS.md"
                    if name == "parent-link"
                    else base / "AGENTS.md"
                    if name == "ancestor-link"
                    else base / "sibling" / "AGENTS.md"
                    if name == "sibling-link"
                    else base / "private.txt"
                )
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b"fixture\n")
                candidate.symlink_to(target)
            elif name.startswith("dangling-"):
                candidate.symlink_to(
                    root / "missing" if name.endswith("inside") else base / "missing"
                )
            elif name == "chain":
                (root / "notes").write_bytes(b"fixture\n")
                (root / "link").symlink_to("notes")
                candidate.symlink_to("link")
            elif name in ("regular", "workspace-link", "inaccessible"):
                candidate.write_bytes(b"fixture\n")
                if name == "inaccessible":
                    root.chmod(0)
                if name == "workspace-link":
                    alias = base / "alias"
                    alias.symlink_to(root)
                    root = alias
            try:
                result = {
                    "accepted": True,
                    "value": projection(load_project_instructions(root)),
                }
            except ProjectInstructionsError as error:
                result = {
                    "accepted": False,
                    "kind": type(error).__name__,
                    "detail": str(error),
                }
            finally:
                if name == "inaccessible":
                    root.chmod(0o700)
            path_cases.append({"name": name, "observation": result})
    return {
        "format": "nebula.assistant-project-instructions/v1",
        "content_cases": cases,
        "path_cases": path_cases,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(collect_project_instructions(), ensure_ascii=False, indent=2) + "\n"
    )
