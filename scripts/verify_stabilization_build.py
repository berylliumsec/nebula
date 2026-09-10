"""Read-only integrity gate for a same-commit production web/Core candidate."""

import argparse
import hashlib
import json
import re
from pathlib import Path


def verify_web_build(directory: Path, expected_commit: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", expected_commit):
        raise ValueError("An exact 40-character commit is required")
    manifest = json.loads((directory / "web-build.json").read_text())
    if (
        manifest.get("schema") != "nebula.web-build/v1"
        or manifest.get("commit") != expected_commit
        or manifest.get("dirty") is not False
    ):
        raise ValueError("Web build identity is missing, dirty or mismatched")
    assets = manifest.get("assets", {})
    actual = {
        file.relative_to(directory).as_posix()
        for file in directory.rglob("*")
        if file.is_file() and file.name != "web-build.json"
    }
    if not {"index.html", "sw.js"} <= assets.keys() or set(assets) != actual:
        raise ValueError("Web build manifest does not cover the exact asset set")
    for name, expected in assets.items():
        file = directory / name
        if file.is_symlink() or not file.resolve().is_relative_to(directory.resolve()):
            raise ValueError("Web assets must stay inside the candidate directory")
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Web asset hash mismatch: {name}")
    return manifest


def verify_candidate(
    directory: Path, core_identity: Path, expected_commit: str
) -> dict:
    web = verify_web_build(directory, expected_commit)
    core = json.loads(core_identity.read_text())
    if core.get("commit") != expected_commit or core.get("build_timestamp") != web.get(
        "builtAt"
    ):
        raise ValueError("Core and web assets were not built with the same identity")
    return {
        "commit": expected_commit,
        "built_at": web["builtAt"],
        "web_assets": len(web["assets"]),
        "core": core,
        "native_acceptance": "required separately; this check does not launch the package",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--core-identity", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            verify_candidate(args.web, args.core_identity, args.commit), indent=2
        )
    )
