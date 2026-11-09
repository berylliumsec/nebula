"""Record actual native build inputs and emit the single-journey path contract.

Run after genuine DEB build/extraction in the same CI job as the GUI case.
This helper never builds, installs, relabels, or edits a runtime.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import test_packaged_runtime_continuity as driver


def write_new(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
    path.chmod(0o600)


def record_build(
    *,
    source: Path,
    source_commit: str,
    desktop: Path,
    connector: Path,
    resources: Path,
    build_command: Path,
    build_log: Path,
    output: Path,
):
    source, desktop, connector, resources, build_command, build_log = (
        path.resolve(strict=True)
        for path in (source, desktop, connector, resources, build_command, build_log)
    )
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("build source must be an exact full Git commit")
    observed = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if observed != source_commit:
        raise ValueError("actual native source checkout differs from its build pin")
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            "src",
            "ui/src",
            "ui/src-tauri/src",
            "ui/src-tauri/Cargo.toml",
            "ui/src-tauri/Cargo.lock",
            "ui/src-tauri/tauri.conf.json",
            "ui/package.json",
            "ui/package-lock.json",
        ],
        check=True,
        capture_output=True,
    )
    if not build_command.stat().st_size or not build_log.stat().st_size:
        raise ValueError("actual native build command/log evidence is missing")
    config_path = source / "ui/src-tauri/tauri.conf.json"
    config = json.loads(config_path.read_bytes())
    frontend = config["build"]["frontendDist"]
    if not isinstance(frontend, str) or "://" in frontend:
        raise ValueError("native frontend input must be the actual local frontendDist")
    frontend_index = (config_path.parent / frontend / "index.html").resolve(strict=True)
    bundle = resources / "nebula-runtime"
    manifest = driver.verify_bundle(bundle)
    if (
        driver.digest(frontend_index)
        != manifest["files"][manifest["ui_directory"] + "/index.html"]
    ):
        raise ValueError(
            "compiled native frontendDist differs from the retained UI input"
        )
    sibling = desktop.parent / "nebula-core"
    if (
        not desktop.is_file()
        or not os.access(desktop, os.X_OK)
        or sibling.resolve(strict=True).parent != desktop.parent
        or driver.digest(sibling) != driver.digest(connector)
    ):
        raise ValueError(
            "extracted native executable/sidecar differs from its actual build"
        )
    receipt = {
        "format": "nebula.native-build/v1",
        "source_commit": observed,
        "source_root": str(source),
        "desktop_path": str(desktop),
        "connector_path": str(sibling),
        "bundle_directory": str(bundle),
        "resource_directory": str(resources),
        "desktop_sha256": driver.digest(desktop),
        "connector_sha256": driver.digest(connector),
        "bundle_manifest_sha256": driver.digest(bundle / "nebula-runtime.json"),
        "frontend_index_sha256": driver.digest(frontend_index),
        "frontend_index": str(frontend_index),
        "tauri_config_sha256": driver.digest(config_path),
        "bundle_version": manifest["version"],
        "build_command_file": str(build_command),
        "build_command_sha256": driver.digest(build_command),
        "build_log": str(build_log),
        "build_log_sha256": driver.digest(build_log),
    }
    write_new(output, receipt)
    return receipt


def write_inputs(
    *,
    baseline: Path,
    candidate: Path,
    output: Path,
    public_model_cache: Path | None = None,
):
    paths, manifests = {}, {}
    for prefix, path in (("baseline", baseline), ("candidate", candidate)):
        path = path.resolve(strict=True)
        receipt = json.loads(path.read_bytes())
        for name, field in (
            ("core", "connector_path"),
            ("desktop", "desktop_path"),
            ("bundle", "bundle_directory"),
            ("resources", "resource_directory"),
        ):
            paths[prefix + "_" + name] = Path(receipt[field]).resolve(strict=True)
        paths[prefix + "_provenance"] = path
        manifests[prefix] = driver.verify_bundle(paths[prefix + "_bundle"])
    driver.verify_release_pair(manifests)
    driver.verify_native_provenance(SimpleNamespace(**paths), manifests)
    values = {name: str(path) for name, path in paths.items()}
    if public_model_cache is not None:
        values["public_model_cache"] = str(public_model_cache.resolve(strict=True))
    write_new(output, values)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    record = commands.add_parser("record-build")
    for name in (
        "source",
        "desktop",
        "connector",
        "resources",
        "build-command",
        "build-log",
        "output",
    ):
        record.add_argument("--" + name, type=Path, required=True)
    record.add_argument("--source-commit", required=True)
    inputs = commands.add_parser("write-inputs")
    for name in ("baseline", "candidate", "output"):
        inputs.add_argument("--" + name, type=Path, required=True)
    inputs.add_argument("--public-model-cache", type=Path)
    args = vars(parser.parse_args())
    operation = args.pop("operation")
    value = (
        record_build(**args) if operation == "record-build" else write_inputs(**args)
    )
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
