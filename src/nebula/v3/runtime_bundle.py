"""Construct immutable runtime payloads for source launches and installers.

Source launches retain Nebula's code and assets but use the selected Python
environment. Frozen bundles contain their interpreter and dependencies in Core.
Neither mode reads Nebula code or UI from the replaced application after launch.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

from .database import CURRENT_SCHEMA_VERSION
from .deployment_protocol import ReleaseManifest
from .release_store import MANIFEST_NAME, ReleaseStore
from .version import __version__


def load_compatibility() -> dict:
    """Return reviewed ranges only while their exact migration tree matches.

    A migration addition or edit requires a new explicit compatibility review;
    a higher head number alone never establishes additive compatibility.
    """
    root = Path(__file__).parent
    payload = json.loads((root / "runtime-compatibility.json").read_text())
    digest = hashlib.sha256()
    for path in sorted((root / "migrations/versions").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if payload.get(
        "format"
    ) != "nebula.runtime-compatibility/v1" or digest.hexdigest() != payload.get(
        "migration_tree_sha256"
    ):
        raise ValueError(
            "Runtime migrations changed; review runtime-compatibility.json before packaging"
        )
    manifest = payload["manifest"]
    if manifest["schema_family"] != CURRENT_SCHEMA_VERSION:
        raise ValueError("Runtime schema family requires a compatibility review")
    return dict(manifest)


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


def _copy_runtime_tree(source: Path, destination: Path) -> None:
    """Materialize a complete onedir tree without external filesystem links."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError("The frozen runtime must be a real onedir directory")
    source = source.resolve(strict=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        selected = path
        if path.is_symlink():
            try:
                selected = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError(
                    f"Invalid runtime dependency link: {relative}"
                ) from error
            if not selected.is_relative_to(source):
                raise ValueError(
                    f"Runtime dependency link escapes its bundle: {relative}"
                )
            if selected.is_dir():
                raise ValueError(
                    f"Runtime directory symlinks are unsupported: {relative}"
                )
        if selected.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(selected, target)
        elif not selected.is_dir():
            raise ValueError(f"Unsupported runtime dependency: {relative}")


def complete_bundle(
    directory: Path, *, version: str | None = None
) -> tuple[ReleaseManifest, str]:
    """Hash the final executable, UI, source, libraries and optional resources."""
    files = {
        path.relative_to(directory).as_posix(): file_digest(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and path.name not in {MANIFEST_NAME, "nebula-runtime.sha256", ".gitignore"}
    }
    identity = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()[:16]
    manifest = ReleaseManifest(
        version=version or f"{__version__}+{identity}",
        **load_compatibility(),
        core_executable="bin/nebula-core",
        ui_directory="ui",
        files=files,
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
    runtime_directory: Path,
    resources: dict[str, Path] | None = None,
    version: str | None = None,
) -> tuple[ReleaseManifest, str]:
    internal = runtime_directory / "_internal"
    if internal.is_symlink() or not internal.is_dir():
        raise ValueError(
            "A retained frozen runtime requires its complete onedir _internal tree"
        )
    if (
        executable.is_symlink()
        or executable.parent.resolve() != runtime_directory.resolve()
    ):
        raise ValueError("The frozen executable must belong to its onedir runtime root")
    if not executable.is_file():
        raise ValueError("The onedir runtime executable is missing")
    if destination.exists() and any(
        path.name != ".gitignore" for path in destination.iterdir()
    ):
        raise ValueError("Build runtime bundles in an empty directory")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "bin").mkdir()
    _copy_runtime_tree(runtime_directory, destination / "bin")
    installed_executable = destination / "bin" / executable.name
    if installed_executable.name != "nebula-core":
        if (destination / "bin/nebula-core").exists():
            raise ValueError(
                "The runtime already contains the reserved Core executable path"
            )
        installed_executable.rename(destination / "bin/nebula-core")
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
        "elif sys.argv[1:] == ['runtime-migrate', '--bootstrap-stdin']:\n"
        "    from nebula.v3.runtime_worker import migration_main as main\n"
        "else:\n    from nebula.v3.cli import main\nmain()\n"
    )
    executable.chmod(0o755)
    return complete_bundle(destination, version=version)
