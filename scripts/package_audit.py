#!/usr/bin/env python3
"""Fail-closed inspection for the frozen Nebula 3 Core artifact."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable


class ArtifactAuditError(RuntimeError):
    """The artifact could not be proven to satisfy its package boundary."""


FORBIDDEN_MODULES = (
    # GUI bindings are never part of the headless Core sidecar.
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    # Unused heavy in-process model/document stacks are outside the Core boundary.
    "IPython",
    "accelerate",
    "cloudpickle",
    "cv2",
    "dask",
    "ddgs",
    "distributed",
    "duckduckgo_search",
    "faker",
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
    "nltk",
    "notebook",
    "numba",
    "ollama",
    "pandas",
    "pexpect",
    "prompt_toolkit",
    "psutil",
    "qdarkstyle",
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
    "pyi_rth_nltk",
    "ruff",
    "sphinx",
    "tkinter",
    "aiosqlite.tests",
    "greenlet.tests",
)

REQUIRED_MEMBERS = (
    ("nebula.v3.cli",),
    ("nebula.v3.runtime_worker",),
    ("nebula.v3.runtime_launcher",),
    ("nebula.v3.runtime_service",),
    ("nebula/v3/runtime-compatibility.json",),
    ("nebula.v3.mcp_gateway",),
    # Tool-output regex searches use the third-party timeout-capable engine.
    # A frozen Core must not pass audit if that runtime dependency is absent.
    ("regex",),
    ("reportlab",),
    ("PIL",),
    # URL knowledge rendering must be importable from the frozen Core and its
    # bundled Node driver must remain executable after extraction.
    ("playwright.sync_api",),
    ("playwright/driver/node",),
    # PostgreSQL is a supported Core database, including in frozen releases.
    ("psycopg",),
    ("psycopg_binary",),
    ("sqlalchemy.dialects.postgresql.psycopg",),
    ("sqlalchemy.ext.asyncio",),
    ("sqlalchemy.dialects.sqlite.aiosqlite",),
    ("aiosqlite",),
    ("greenlet",),
    ("nebula/v3/BUILD_INFO.json", "nebula.v3.BUILD_INFO.json"),
    ("nebula/v3/migrations/script.py.mako",),
    # The Kali workstation image build copies these files out of the bundle.
    ("nebula/v3/kali_tool_inventory.py",),
    ("nebula/v3/egress_helper.py",),
    ("nebula/v3/public_ip_update.py",),
    ("nebula/v3/terminal_entrypoint.sh",),
    # The harness MCP gateway shim runs from this bundled source file.
    ("nebula/v3/mcp_gateway.py",),
    ("nebula/v3/operator_help.md",),
    ("nebula/v3/product_knowledge.sqlite3.b85",),
    ("nebula/v3/diagnostic_guidance.json",),
    ("nebula/v3/assistant_destinations.json",),
    ("nebula/v3/report_assets/fonts/NotoSans-Regular.ttf",),
    ("nebula/v3/report_assets/fonts/NotoSans-Bold.ttf",),
    ("nebula/v3/report_assets/fonts/NotoSansMono-Regular.ttf",),
    ("nebula/v3/report_assets/fonts/NotoSansMono-Bold.ttf",),
    ("nebula/v3/report_assets/fonts/OFL.txt",),
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
PLAYWRIGHT_RUNTIME_MANIFEST = "nebula-playwright-runtime.json"
NOTEBOOK_RUNTIME_MANIFEST = "nebula-notebook-runtime.json"


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
        or "tests" in member.replace("\\", "/").split("/")
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


def _pyinstaller_members(binary: Path) -> set[str]:
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
    return members


def inspect_pyinstaller_binary(binary: Path) -> dict[str, object]:
    """Inspect both the outer CArchive and embedded PYZ, failing if unreadable."""
    binary = binary.resolve()
    report = validate_members(_pyinstaller_members(binary))
    report["artifact"] = str(binary)
    report["size_bytes"] = binary.stat().st_size
    return report


def inspect_pyinstaller_directory(
    directory: Path, executable_name: str
) -> dict[str, object]:
    """Audit the executable plus every dependency in a PyInstaller onedir tree."""
    if directory.is_symlink() or not directory.is_dir():
        raise ArtifactAuditError("the runtime must be a real onedir directory")
    directory = directory.resolve()
    executable = directory / executable_name
    if executable.parent != directory or executable.is_symlink():
        raise ArtifactAuditError("the runtime executable must be in its onedir root")
    members = _pyinstaller_members(executable)
    size = 0
    files = 0
    for path in directory.rglob("*"):
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ArtifactAuditError(
                    "runtime dependency link is invalid"
                ) from error
            if not target.is_relative_to(directory) or target.is_dir():
                raise ArtifactAuditError(
                    "runtime dependency links must target files inside the onedir tree"
                )
        if not path.is_file():
            if not path.is_dir():
                raise ArtifactAuditError("runtime dependencies must be regular files")
            continue
        relative = path.relative_to(directory)
        normalized = relative.as_posix()
        if relative.parts[0] == "_internal":
            normalized = relative.relative_to("_internal").as_posix()
        members.add(normalized)
        files += 1
        size += path.stat().st_size
    report = validate_members(members)
    report.update(
        artifact=str(directory),
        runtime_executable=executable_name,
        file_count=files,
        size_bytes=size,
        format="pyinstaller-onedir",
    )
    return report


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_path(value: object) -> str:
    if not isinstance(value, str):
        raise ArtifactAuditError("retained runtime asset path is invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or "\\" in value
        or "\x00" in value
        or str(path) != value
    ):
        raise ArtifactAuditError("retained runtime asset path is invalid")
    return value


def _verified_installer_bundles(files: list[Path]) -> dict[Path, dict[str, str]]:
    """Verify retained bytes before permitting the exact embedded Node driver.

    Keep this audit in the standard library so clean installer hosts do not
    need Core's interpreter dependencies to inspect a package.
    """
    manifests = [path for path in files if path.name == "nebula-runtime.json"]
    if len(manifests) > 1:
        raise ArtifactAuditError("installer contains multiple retained runtime bundles")
    result = {}
    for path in manifests:
        root = path.parent
        marker = root / "nebula-runtime.sha256"
        if (
            root.is_symlink()
            or path.is_symlink()
            or path.stat().st_size > 16 * 1024**2
            or marker.is_symlink()
            or not marker.is_file()
            or marker.stat().st_size > 100
        ):
            raise ArtifactAuditError("retained runtime manifest is invalid")
        try:
            expected_manifest = marker.read_text(encoding="utf-8").strip()
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ArtifactAuditError("retained runtime manifest is invalid") from error
        if not re.fullmatch(
            r"[0-9a-f]{64}", expected_manifest
        ) or not hmac.compare_digest(_file_sha256(path), expected_manifest):
            raise ArtifactAuditError(
                "retained runtime manifest failed its digest check"
            )
        assets = manifest.get("files") if isinstance(manifest, dict) else None
        if (
            not isinstance(assets, dict)
            or not 1 <= len(assets) <= 100_000
            or manifest.get("format") != "nebula.runtime-bundle/v1"
            or manifest.get("core_executable") != "bin/nebula-core"
            or "bin/nebula-core" not in assets
            or "bin/_internal/playwright/driver/node" not in assets
        ):
            raise ArtifactAuditError(
                "retained runtime manifest has an invalid contract"
            )
        actual = set()
        for asset in root.rglob("*"):
            if asset.is_symlink():
                raise ArtifactAuditError("retained runtime contains a symlinked asset")
            if asset.is_dir():
                continue
            if not asset.is_file():
                raise ArtifactAuditError("retained runtime contains a non-file asset")
            relative = asset.relative_to(root).as_posix()
            if relative not in {"nebula-runtime.json", "nebula-runtime.sha256"} and (
                asset.name != ".gitignore"
            ):
                actual.add(relative)
        if actual != set(assets):
            raise ArtifactAuditError("retained runtime has missing or unlisted assets")
        for relative, expected in assets.items():
            selected = root / _bundle_path(relative)
            if (
                not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not hmac.compare_digest(_file_sha256(selected), expected)
            ):
                raise ArtifactAuditError(
                    "retained runtime asset failed its digest check"
                )
        for relative in ("bin/nebula-core", "bin/_internal/playwright/driver/node"):
            if not os.access(root / relative, os.X_OK):
                raise ArtifactAuditError(
                    "retained runtime executable is not executable"
                )
        result[root] = assets
    return result


def _installer_resource_manifest(
    files: list[Path], name: str, resource: str, bundles: dict[Path, dict[str, str]]
) -> Path | None:
    """Allow a legacy resource path alongside its identical retained copy."""
    candidates = [path for path in files if path.name == name]
    outside, retained = [], []
    for path in candidates:
        owner = next((root for root in bundles if path.is_relative_to(root)), None)
        if owner is None:
            outside.append(path)
        elif path.relative_to(owner).as_posix() == f"resources/{resource}/{name}":
            retained.append((path, owner))
        else:
            raise ArtifactAuditError(
                "retained resource manifest has an unexpected path"
            )
    if len(outside) > 1 or len(retained) > 1:
        raise ArtifactAuditError("installer contains ambiguous resource manifests")
    if outside and retained:
        # Preserve current installer consumers while checking the complete copy,
        # including nonexecutable assets, against the retained file manifest.
        source = outside[0].parent
        retained_path, owner = retained[0]
        prefix = f"resources/{resource}/"
        expected = {
            path.removeprefix(prefix): digest
            for path, digest in bundles[owner].items()
            if path.startswith(prefix)
        }
        actual = {}
        for asset in source.rglob("*"):
            if asset.is_symlink():
                raise ArtifactAuditError(
                    "duplicate installer resource contains a symlink"
                )
            if asset.is_dir():
                continue
            if not asset.is_file():
                raise ArtifactAuditError(
                    "duplicate installer resource contains a non-file"
                )
            if asset.name != ".gitignore":
                actual[asset.relative_to(source).as_posix()] = _file_sha256(asset)
        if actual != expected:
            raise ArtifactAuditError(
                "duplicate installer resource differs from retained bytes"
            )
        return retained_path
    if retained:
        return retained[0][0]
    return outside[0] if outside else None


def inspect_installer_tree(root: Path) -> dict[str, object]:
    """Audit an extracted native installer rather than trusting its container."""

    root = root.resolve()
    if not root.is_dir():
        raise ArtifactAuditError(f"installer tree does not exist: {root}")
    files = [path for path in root.rglob("*") if path.is_file()]
    relative = [f"/{path.relative_to(root).as_posix()}" for path in files]
    bundles = _verified_installer_bundles(files)
    retained_drivers = {
        bundle / "bin/_internal/playwright/driver/node" for bundle in bundles
    }
    forbidden = sorted(
        value
        for path, value in zip(files, relative)
        if (
            path.name in FORBIDDEN_INSTALLER_BINARIES
            and "/notebook-runtime/" not in value
            and path not in retained_drivers
        )
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
    playwright_manifest = _installer_resource_manifest(
        files, PLAYWRIGHT_RUNTIME_MANIFEST, "playwright-browsers", bundles
    )
    if playwright_manifest is None:
        missing.append("bundled Playwright Chromium manifest")
    notebook_manifest = _installer_resource_manifest(
        files, NOTEBOOK_RUNTIME_MANIFEST, "notebook-runtime", bundles
    )
    if notebook_manifest is None:
        missing.append("bundled notebook runtime manifest")
    if missing:
        raise ArtifactAuditError(
            f"required installer content absent: {', '.join(missing)}"
        )
    manifest_path = playwright_manifest
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactAuditError(
            "bundled Playwright Chromium manifest is invalid"
        ) from exc
    executables = manifest.get("executables")
    executable_sha256 = manifest.get("executable_sha256")
    licenses = manifest.get("licenses")
    sbom = manifest.get("sbom")
    provenance = manifest.get("provenance")
    if (
        manifest.get("schema") != 1
        or manifest.get("browser") != "chromium"
        or not isinstance(manifest.get("playwright_version"), str)
        or not manifest["playwright_version"]
        or not isinstance(manifest.get("target"), str)
        or not manifest["target"]
        or not isinstance(manifest.get("payload_bytes"), int)
        or manifest["payload_bytes"] <= 0
        or not isinstance(executables, list)
        or not executables
        or not all(isinstance(value, str) and value for value in executables)
        or not isinstance(executable_sha256, dict)
        or set(executable_sha256) != set(executables)
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in executable_sha256.values()
        )
        or not isinstance(licenses, list)
        or not licenses
        or not all(isinstance(value, str) and value for value in licenses)
        or not isinstance(sbom, str)
        or not sbom
        or not isinstance(provenance, dict)
        or provenance.get("installer") != "python -m playwright install chromium"
        or provenance.get("target") != manifest.get("target")
    ):
        raise ArtifactAuditError(
            "bundled Playwright Chromium manifest has an invalid contract"
        )
    runtime_root = manifest_path.parent.resolve()
    for relative in executables:
        executable = (runtime_root / relative).resolve()
        if (
            not executable.is_relative_to(runtime_root)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ArtifactAuditError(
                "bundled Playwright Chromium executable is absent or not executable"
            )
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, executable_sha256[relative]):
            raise ArtifactAuditError(
                "bundled Playwright Chromium executable failed its digest check"
            )
    for relative in licenses:
        license_file = (runtime_root / relative).resolve()
        if (
            not license_file.is_relative_to(runtime_root)
            or not license_file.is_file()
            or license_file.stat().st_size == 0
        ):
            raise ArtifactAuditError(
                "bundled Playwright Chromium license payload is absent"
            )
    sbom_path = (runtime_root / sbom).resolve()
    if (
        not sbom_path.is_relative_to(runtime_root)
        or not sbom_path.is_file()
        or sbom_path.stat().st_size == 0
    ):
        raise ArtifactAuditError("bundled Playwright Chromium SBOM is absent")
    try:
        sbom_payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactAuditError("bundled Playwright Chromium SBOM is invalid") from exc
    if sbom_payload.get("spdxVersion") != "SPDX-2.3":
        raise ArtifactAuditError("bundled Playwright Chromium SBOM is invalid")
    notebook_root = notebook_manifest.parent.resolve()
    try:
        notebook = json.loads(notebook_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactAuditError(
            "bundled notebook runtime manifest is invalid"
        ) from exc
    if (
        notebook.get("schema") != 1
        or notebook.get("target") != "x86_64-unknown-linux-gnu"
    ):
        raise ArtifactAuditError("bundled notebook runtime manifest is invalid")
    notebook_executables = notebook.get("executables")
    if not isinstance(notebook_executables, dict) or set(notebook_executables) != {
        "python/bin/python3",
        "uv",
    }:
        raise ArtifactAuditError("bundled notebook runtime executables are invalid")
    for relative_path, expected in notebook_executables.items():
        executable = (notebook_root / relative_path).resolve()
        if (
            not executable.is_relative_to(notebook_root)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
            or not isinstance(expected, str)
            or not hmac.compare_digest(
                hashlib.sha256(executable.read_bytes()).hexdigest(), expected
            )
        ):
            raise ArtifactAuditError(
                "bundled notebook runtime executable failed verification"
            )
    worker = notebook_root / "worker.py"
    if not worker.is_file() or hashlib.sha256(
        worker.read_bytes()
    ).hexdigest() != notebook.get("worker_sha256"):
        raise ArtifactAuditError("bundled notebook worker failed verification")
    return {
        "status": "ok",
        "tree": str(root),
        "file_count": len(files),
        "forbidden_path_count": 0,
        "retained_bundle_count": len(bundles),
        "playwright_executable_count": len(executables),
        "playwright_license_count": len(licenses),
        "notebook_executable_count": len(notebook_executables),
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
