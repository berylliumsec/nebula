"""Construct immutable runtime payloads for source launches and installers.

Source launches retain Nebula's code and assets but use the selected Python
environment. Frozen bundles contain their interpreter and dependencies in Core.
Neither mode reads Nebula code or UI from the replaced application after launch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys

from .database import CURRENT_SCHEMA_VERSION
from .deployment_protocol import ReleaseManifest
from .release_store import MANIFEST_NAME, ReleaseStore
from .version import __version__

RUNTIME_FEATURES = ["chat.accept/v1", "chat.resume/v1", "project.handoff/v1"]


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Runtime asset directory is unavailable: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(
            part in {"__pycache__", ".git", "node_modules"} for part in relative.parts
        ):
            continue
        if path.suffix in {".pyc", ".pyo"} or path.name.endswith(
            (".js.map", ".css.map")
        ):
            continue
        if path.is_symlink():
            raise ValueError(f"Runtime bundles cannot contain symlinks: {relative}")
        if path.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def complete_bundle(
    directory: Path, *, version: str | None = None
) -> tuple[ReleaseManifest, str]:
    """Hash the final executable, UI, source, libraries and optional resources."""
    files = {
        path.relative_to(directory).as_posix(): file_digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name not in {MANIFEST_NAME, "nebula-runtime.sha256"}
    }
    identity = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()[:16]
    migrations = directory / "python/nebula/v3/migrations/versions"
    heads = sorted(path.stem for path in migrations.glob("*.py"))
    manifest = ReleaseManifest(
        version=version or f"{__version__}+{identity}",
        schema_family=CURRENT_SCHEMA_VERSION,
        schema_min=CURRENT_SCHEMA_VERSION,
        schema_max=CURRENT_SCHEMA_VERSION,
        additive_migrations=True,
        migration_head=heads[-1] if heads else "0040_runtime_chat_cursor",
        core_executable="bin/nebula-core",
        ui_directory="ui",
        files=files,
        features=RUNTIME_FEATURES,
    )
    (directory / MANIFEST_NAME).write_text(manifest.model_dump_json() + "\n")
    digest = file_digest(directory / MANIFEST_NAME)
    (directory / "nebula-runtime.sha256").write_text(digest + "\n")
    ReleaseStore.verify(directory, expected_manifest_sha256=digest)
    return manifest, digest


def build_frozen_bundle(
    executable: Path,
    ui_directory: Path,
    destination: Path,
    *,
    resources: dict[str, Path] | None = None,
    version: str | None = None,
) -> tuple[ReleaseManifest, str]:
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "bin").mkdir()
    shutil.copy2(executable, destination / "bin/nebula-core")
    _copy_tree(ui_directory, destination / "ui")
    for name, source in (resources or {}).items():
        if name not in {"playwright-browsers", "notebook-runtime", "security-proxy"}:
            raise ValueError("Unsupported runtime resource directory")
        _copy_tree(source, destination / "resources" / name)
    return complete_bundle(destination, version=version)


def build_source_bundle(
    source_package: Path,
    ui_directory: Path,
    destination: Path,
    *,
    version: str | None = None,
) -> tuple[ReleaseManifest, str]:
    destination.mkdir(parents=True, exist_ok=False)
    _copy_tree(source_package, destination / "python/nebula")
    _copy_tree(ui_directory, destination / "ui")
    (destination / "bin").mkdir()
    executable = destination / "bin/nebula-core"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))\n"
        "if sys.argv[1:] == ['runtime-worker', '--bootstrap-stdin']:\n"
        "    from nebula.v3.runtime_worker import main\n"
        "else:\n    from nebula.v3.cli import main\nmain()\n"
    )
    executable.chmod(0o755)
    return complete_bundle(destination, version=version)
