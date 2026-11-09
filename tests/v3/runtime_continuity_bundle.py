"""Self-contained source bundles for disposable continuity acceptance.

These are source-runtime fixtures, not distribution artifacts. Each version owns
its Python interpreter, stdlib, installed dependencies, source and production UI;
workers never import the checkout or its virtual environment. System ABI shared
libraries remain host prerequisites, as they do for a locally installed Python.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import sysconfig

from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.release_store import MANIFEST_NAME, ReleaseStore


@dataclass(frozen=True)
class AcceptanceBundle:
    version: str
    path: Path
    manifest_sha256: str
    source_sha256: str
    index_sha256: str


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in {"__pycache__", ".git", "site-packages", "test", "tests"}
        or name.endswith((".pyc", ".pth", ".egg-link"))
    }


def copy_runtime(destination: Path, dependencies: Path | None = None) -> None:
    """Copy, rather than symlink, the selected interpreter and dependencies."""
    interpreter = destination / "python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    shutil.copy2(Path(sys.executable).resolve(), interpreter)
    library = destination / "python" / "lib"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    shutil.copytree(sysconfig.get_path("stdlib"), library / version, ignore=_ignore)
    for name in ("libpython3.so", f"lib{version}.so.1.0"):
        selected = Path(sys.base_prefix) / "lib" / name
        if selected.exists():
            shutil.copy2(selected.resolve(), library / name)
    packages = dependencies or Path(sysconfig.get_path("purelib"))
    # The test runner can be outside the project's virtualenv. Never silently
    # snapshot a different interpreter environment.
    if not (packages / "chromadb").is_dir():
        raise ValueError("select the Nebula virtualenv dependency directory explicitly")
    shutil.copytree(packages, destination / "dependencies", ignore=_ignore)


def build_bundle(
    directory: Path,
    *,
    version: str,
    source: Path,
    ui: Path,
    dependencies: Path | None = None,
    runtime_template: Path | None = None,
) -> AcceptanceBundle:
    """Build an immutable fixture from explicitly selected source and UI trees."""
    if not (ui / "index.html").is_file() or not (source / "nebula" / "v3").is_dir():
        raise ValueError("a production UI build and complete Nebula source are required")
    directory.mkdir(parents=True, exist_ok=False)
    if runtime_template is None:
        copy_runtime(directory, dependencies)
    else:
        # Separate inodes prevent a version's permission or content change from
        # modifying another version's dependency snapshot.
        for name in ("python", "dependencies"):
            shutil.copytree(runtime_template / name, directory / name)
    shutil.copytree(source / "nebula", directory / "source" / "nebula", ignore=_ignore)
    shutil.copytree(ui, directory / "ui", ignore=_ignore)
    worker = Path(__file__).with_name("runtime_continuity_worker.py")
    shutil.copy2(worker, directory / "acceptance_worker.py")
    (directory / "entry.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "root=Path(__file__).resolve().parent\n"
        "sys.path[:0]=[str(root/'source'),str(root/'dependencies'),str(root)]\n"
        "from acceptance_worker import main\nmain()\n"
    )
    executable = directory / "nebula-core"
    executable.write_text(
        '#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'export LD_LIBRARY_PATH="$root/python/lib"\n'
        'exec "$root/python/bin/python3" -I -S "$root/entry.py" "$@"\n'
    )
    executable.chmod(0o755)
    files = {
        str(path.relative_to(directory)): sha256(path)
        for path in sorted(directory.rglob("*")) if path.is_file()
    }
    source_hash = hashlib.sha256(json.dumps({
        name: digest for name, digest in files.items() if name.startswith("source/")
    }, sort_keys=True).encode()).hexdigest()
    manifest = ReleaseManifest(
        version=version, schema_family=5, schema_min=5, schema_max=5,
        additive_migrations=True, migration_head="0041_handoff_admission",
        core_executable="nebula-core", ui_directory="ui", files=files,
        features=["chat.accept/v1", "chat.resume/v1", "project.handoff/v1"],
    )
    (directory / MANIFEST_NAME).write_text(manifest.model_dump_json())
    digest = sha256(directory / MANIFEST_NAME)
    ReleaseStore.verify(directory, expected_manifest_sha256=digest)
    return AcceptanceBundle(version, directory, digest, source_hash, files["ui/index.html"])


def retained_bundle(bundle: AcceptanceBundle, retained: ReleaseStore) -> AcceptanceBundle:
    _, path = retained.install(bundle.path, expected_manifest_sha256=bundle.manifest_sha256)
    return AcceptanceBundle(
        bundle.version, path, bundle.manifest_sha256, bundle.source_sha256, bundle.index_sha256
    )


def remove_owned_tree(path: Path) -> None:
    if not path.exists():
        return
    for directory, _children, _files in os.walk(path):
        Path(directory).chmod(0o700)
    shutil.rmtree(path)
