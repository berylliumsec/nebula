"""Build a self-contained client wheel/sdist without Core's build backend."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
CLIENT_FILES = (
    "__init__.py",
    "chat_cli.py",
    "terminal/__init__.py",
    "terminal/transport.py",
    "terminal/state.py",
    "terminal/ui.py",
    "terminal/tui.py",
    "terminal/shell.py",
    "terminal/settings.py",
    "terminal/workflows.py",
    "terminal/attachments.py",
)


def stage(destination: Path, root: Path = ROOT) -> None:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(Version(metadata["tool"]["poetry"]["version"]))
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("pyproject.toml", "README.md"):
        shutil.copyfile(root / "clients" / "python" / name, destination / name)
    shutil.copyfile(root / "LICENSE.md", destination / "LICENSE.md")
    (destination / "VERSION").write_text(version + "\n", encoding="ascii")
    for relative in CLIENT_FILES:
        source = root / "src" / "nebula" / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"Missing regular client source: {relative}")
        target = destination / "src" / "nebula" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "dist" / "terminal-client"
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="Use already installed build dependencies.",
    )
    args = parser.parse_args()
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nebula-client-build-") as temporary:
        staged = Path(temporary)
        stage(staged)
        command = [sys.executable, "-m", "build", "--outdir", str(output)]
        if args.no_isolation:
            command.append("--no-isolation")
        subprocess.run([*command, str(staged)], check=True)
    artifacts = sorted(
        [*output.glob("nebula_client-*.whl"), *output.glob("nebula_client-*.tar.gz")]
    )
    if len(artifacts) != 2:
        raise RuntimeError(
            "Choose a clean output directory; expected exactly one client wheel and source distribution."
        )
    (output / "nebula-client-SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in artifacts
        ),
        encoding="ascii",
    )
    for artifact in artifacts:
        print(artifact)


if __name__ == "__main__":
    main()
