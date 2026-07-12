#!/usr/bin/env python3
"""Fail-closed inspection for the frozen Nebula 3 Core artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable


class ArtifactAuditError(RuntimeError):
    """The artifact could not be proven to satisfy its package boundary."""


FORBIDDEN_MODULES = (
    # Qt and alternate legacy GUI bindings.
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    # Heavy legacy model/document stacks.
    "IPython",
    "PIL",
    "accelerate",
    "chromadb",
    "cloudpickle",
    "cv2",
    "dask",
    "ddgs",
    "distributed",
    "duckduckgo_search",
    "faker",
    "filelock",
    "h5py",
    "jq",
    "langchain",
    "langchain_chroma",
    "langchain_classic",
    "langchain_community",
    "langchain_experimental",
    "langchain_huggingface",
    "langchain_ollama",
    "langchain_openai",
    "matplotlib",
    "nbclient",
    "nbconvert",
    "nbformat",
    "notebook",
    "numba",
    "numpy",
    "ollama",
    "pandas",
    "pexpect",
    "prompt_toolkit",
    "psutil",
    "qdarkstyle",
    "regex",
    "scipy",
    "sentence_transformers",
    "spacy",
    "thinc",
    "tiktoken",
    "torch",
    "transformers",
    "unstructured",
    "whoosh",
    # Test and developer tooling.
    "Cython",
    "black",
    "mypy",
    "_pytest",
    "pytest",
    "ruff",
    "sphinx",
    "tkinter",
    # Nebula 2 application modules.
    "nebula.MainWindow",
    "nebula.ai_notes_pop_up_window",
    "nebula.central_display_area_in_main_window",
    "nebula.chroma_manager",
    "nebula.configuration_manager",
    "nebula.conversation_memory",
    "nebula.document_loader",
    "nebula.help",
    "nebula.image_command_window",
    "nebula.image_display_label",
    "nebula.initial_logic",
    "nebula.nebula",
    "nebula.run_python",
    "nebula.search",
    "nebula.search_replace_dialog",
    "nebula.setup_nebula",
    "nebula.status_update_feed_manager",
    "nebula.suggestions_pop_out_window",
    "nebula.terminal_emulator",
    "nebula.tool_configuration",
    "nebula.user_note_taking",
    "nebula.utilities",
)

REQUIRED_MEMBERS = (
    ("nebula.v3.cli",),
    ("nebula/v3/BUILD_INFO.json", "nebula.v3.BUILD_INFO.json"),
    ("nebula/v3/migrations/script.py.mako",),
    ("ui/dist/index.html",),
    ("licenses/LICENSE.md",),
    ("licenses/THIRD_PARTY_NOTICES.txt",),
)

FORBIDDEN_INSTALLER_PATH_MARKERS = (
    "/PyQt5/",
    "/PyQt6/",
    "/PySide2/",
    "/PySide6/",
    "/torch/",
    "/transformers/",
    "/tests/",
    "/__pycache__/",
)
FORBIDDEN_INSTALLER_BINARIES = {
    "cargo",
    "node",
    "npm",
    "npx",
    "poetry",
    "pytest",
    "python",
    "python3",
    "rustc",
}

FORBIDDEN_RESIDUE_SUFFIXES = (".pyc", ".pyo", ".js.map", ".css.map")


def _normalized(member: str) -> str:
    return member.replace("\\", "/").replace("/", ".")


def _matches_module(member: str, module: str) -> bool:
    normalized = _normalized(member)
    return normalized == module or normalized.startswith(f"{module}.")


def validate_members(members: Iterable[str]) -> dict[str, object]:
    """Validate a freezer member list; useful for both builds and tests."""

    member_set = {str(member) for member in members}
    if not member_set:
        raise ArtifactAuditError("artifact archive contained no inspectable members")
    forbidden = sorted(
        {
            member
            for member in member_set
            for module in FORBIDDEN_MODULES
            if _matches_module(member, module)
        }
    )
    residue = sorted(
        member
        for member in member_set
        if "__pycache__" in member.replace("\\", "/").split("/")
        or member.replace("\\", "/").endswith(FORBIDDEN_RESIDUE_SUFFIXES)
    )
    missing = [
        alternatives
        for alternatives in REQUIRED_MEMBERS
        if not any(candidate in member_set for candidate in alternatives)
    ]
    if forbidden or residue or missing:
        problems = []
        if forbidden:
            problems.append(f"forbidden members: {', '.join(forbidden)}")
        if residue:
            problems.append(f"build residue: {', '.join(residue)}")
        if missing:
            labels = [" or ".join(group) for group in missing]
            problems.append(f"required members absent: {', '.join(labels)}")
        raise ArtifactAuditError("; ".join(problems))
    return {
        "status": "ok",
        "member_count": len(member_set),
        "forbidden_member_count": 0,
    }


def inspect_pyinstaller_binary(binary: Path) -> dict[str, object]:
    """Inspect both the outer CArchive and embedded PYZ, failing if unreadable."""

    binary = binary.resolve()
    if not binary.is_file():
        raise ArtifactAuditError(f"artifact does not exist: {binary}")
    try:
        from PyInstaller.archive.readers import CArchiveReader

        archive = CArchiveReader(str(binary))
        members = set(archive.toc)
        embedded = [name for name in archive.toc if name.endswith(".pyz")]
        if not embedded:
            raise ArtifactAuditError("artifact has no embedded Python archive")
        for name in embedded:
            members.update(archive.open_embedded_archive(name).toc)
    except ArtifactAuditError:
        raise
    except Exception as exc:
        raise ArtifactAuditError(
            f"cannot inspect PyInstaller artifact {binary}"
        ) from exc
    report = validate_members(members)
    report["artifact"] = str(binary)
    report["size_bytes"] = binary.stat().st_size
    return report


def inspect_installer_tree(root: Path) -> dict[str, object]:
    """Audit an extracted native installer rather than trusting its container."""

    root = root.resolve()
    if not root.is_dir():
        raise ArtifactAuditError(f"installer tree does not exist: {root}")
    files = [path for path in root.rglob("*") if path.is_file()]
    relative = [f"/{path.relative_to(root).as_posix()}" for path in files]
    forbidden = sorted(
        value
        for path, value in zip(files, relative)
        if path.name in FORBIDDEN_INSTALLER_BINARIES
        or value.endswith((".pyc", ".pyo", ".js.map", ".css.map"))
        or any(marker in value for marker in FORBIDDEN_INSTALLER_PATH_MARKERS)
    )
    if forbidden:
        raise ArtifactAuditError(f"forbidden installer content: {', '.join(forbidden)}")

    def has_suffix(*suffixes: str) -> bool:
        return any(value.endswith(suffixes) for value in relative)

    missing = []
    if not has_suffix("/nebula-ui"):
        missing.append("nebula-ui")
    if not has_suffix("/nebula-core"):
        missing.append("nebula-core")
    if not has_suffix("/LICENSE", "/LICENSE.md"):
        missing.append("LICENSE")
    if not has_suffix("/THIRD_PARTY_NOTICES.txt"):
        missing.append("THIRD_PARTY_NOTICES.txt")
    if missing:
        raise ArtifactAuditError(
            f"required installer content absent: {', '.join(missing)}"
        )
    return {
        "status": "ok",
        "tree": str(root),
        "file_count": len(files),
        "forbidden_path_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, nargs="?")
    parser.add_argument("--tree", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if (arguments.artifact is None) == (arguments.tree is None):
            parser.error("pass exactly one Core artifact or --tree DIRECTORY")
        report = (
            inspect_installer_tree(arguments.tree)
            if arguments.tree is not None
            else inspect_pyinstaller_binary(arguments.artifact)
        )
    except ArtifactAuditError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
