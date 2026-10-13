"""Stage the pinned, self-contained notebook runtime for Linux installers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging" / "notebook" / "runtime-lock.json"
REQUIREMENTS = ROOT / "packaging" / "notebook" / "requirements.lock"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(url: str, sha256: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{sha256}.tar.gz"
    if destination.is_file() and digest(destination) == sha256:
        return destination
    temporary = destination.with_suffix(".partial")
    request = urllib.request.Request(
        url, headers={"User-Agent": "nebula-notebook-build"}
    )
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        temporary.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    if digest(temporary) != sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("notebook runtime artifact digest mismatch")
    os.replace(temporary, destination)
    return destination


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            target = (destination / member.name).resolve(strict=False)
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("notebook runtime archive contains an unsafe path")
            if member.issym():
                link_target = (
                    (destination / member.name)
                    .parent.joinpath(member.linkname)
                    .resolve(strict=False)
                )
                if not link_target.is_relative_to(destination.resolve()):
                    raise RuntimeError(
                        "notebook runtime archive contains an unsafe symlink"
                    )
            if member.islnk():
                link_target = (destination / member.linkname).resolve(strict=False)
                if not link_target.is_relative_to(destination.resolve()):
                    raise RuntimeError(
                        "notebook runtime archive contains an unsafe hardlink"
                    )
        source.extractall(destination, filter="data")


def stage(destination: Path, *, target: str) -> dict[str, object]:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if target != lock["target"]:
        raise RuntimeError(f"notebook runtime is not pinned for {target}")
    cache = ROOT / "build" / "notebook-downloads"
    python_archive = download(lock["python_url"], lock["python_sha256"], cache)
    uv_archive = download(lock["uv_url"], lock["uv_sha256"], cache)
    with tempfile.TemporaryDirectory(
        prefix="nebula-notebook-stage-", dir=ROOT / "build"
    ) as temporary:
        staged = Path(temporary)
        extract(python_archive, staged)
        uv_staging = staged / "uv-unpack"
        extract(uv_archive, uv_staging)
        uv_candidates = list(uv_staging.rglob("uv"))
        if len(uv_candidates) != 1:
            raise RuntimeError("uv release archive does not contain one uv executable")
        shutil.copy2(uv_candidates[0], staged / "uv")
        (staged / "uv").chmod(0o755)
        shutil.rmtree(uv_staging)
        python = staged / "python" / "bin" / "python3"
        if not python.is_file():
            raise RuntimeError("portable Python archive does not contain python3")
        env = dict(os.environ)
        env["UV_PYTHON_DOWNLOADS"] = "never"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.run(
            [
                str(staged / "uv"),
                "pip",
                "install",
                "--python",
                str(python),
                "--require-hashes",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
            env=env,
        )
        subprocess.run(
            [str(python), "-c", "import ipykernel, jupyter_client"], check=True, env=env
        )
        shutil.copy2(
            ROOT / "packaging" / "notebook" / "worker.py", staged / "worker.py"
        )
        for residue in staged.rglob("__pycache__"):
            if residue.is_dir():
                shutil.rmtree(residue)
        manifest = {
            "schema": 1,
            "target": target,
            "python_version": lock["python_version"],
            "uv_version": lock["uv_version"],
            "python_archive_sha256": lock["python_sha256"],
            "uv_archive_sha256": lock["uv_sha256"],
            "requirements_sha256": digest(REQUIREMENTS),
            "executables": {
                "python/bin/python3": digest(python),
                "uv": digest(staged / "uv"),
            },
            "worker_sha256": digest(staged / "worker.py"),
        }
        (staged / "nebula-notebook-runtime.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staged, destination, symlinks=False)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--target", required=True)
    arguments = parser.parse_args()
    (ROOT / "build").mkdir(exist_ok=True)
    print(json.dumps(stage(arguments.destination, target=arguments.target), indent=2))


if __name__ == "__main__":
    main()
