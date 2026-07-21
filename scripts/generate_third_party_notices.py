#!/usr/bin/env python3
"""Generate notices for installed dependencies in Poetry's main group."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
from pathlib import Path

import tomli
from packaging.utils import canonicalize_name


def locked_main_packages(lockfile: Path) -> set[str]:
    with lockfile.open("rb") as stream:
        lock = tomli.load(stream)
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("poetry.lock does not contain package records")
    result = {
        canonicalize_name(package["name"])
        for package in packages
        if isinstance(package, dict)
        and isinstance(package.get("name"), str)
        and isinstance(package.get("groups"), list)
        and "main" in package["groups"]
    }
    if not result:
        raise RuntimeError("poetry.lock contains no main dependency group")
    return result


def _license_files(distribution: importlib.metadata.Distribution) -> list[Path]:
    selected = []
    for item in distribution.files or ():
        normalized = str(item).replace("\\", "/")
        basename = Path(normalized).name.lower()
        if "/licenses/" in normalized.lower() or basename.startswith(
            ("license", "copying", "notice", "copyright")
        ):
            candidate = Path(distribution.locate_file(item))
            if candidate.is_file():
                selected.append(candidate)
    return sorted(set(selected), key=lambda path: path.as_posix().lower())


def npm_components(root: Path) -> list[dict[str, str]]:
    lock_path = root / "ui" / "package-lock.json"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeError("ui/package-lock.json has no package inventory")
    components = []
    for location, package in sorted(packages.items()):
        if not location or not isinstance(package, dict) or package.get("dev") is True:
            continue
        installed = root / "ui" / location
        if not installed.is_dir():
            continue
        version = package.get("version")
        if not isinstance(version, str):
            raise RuntimeError(f"npm package {location} has no locked version")
        name = location.rsplit("node_modules/", 1)[-1]
        license_name = package.get("license")
        components.append(
            {
                "ecosystem": "npm",
                "name": name,
                "version": version,
                "license": license_name
                if isinstance(license_name, str)
                else "Not declared",
            }
        )
    if not components:
        raise RuntimeError("no installed production npm packages were found")
    return components


def cargo_components(
    root: Path, target: str, *, all_features: bool = True
) -> list[dict[str, str]]:
    command = [
        "cargo",
        "metadata",
        "--locked",
        "--filter-platform",
        target,
        "--format-version",
        "1",
        "--manifest-path",
        str(root / "ui" / "src-tauri" / "Cargo.toml"),
    ]
    if all_features:
        command.insert(3, "--all-features")
    payload = json.loads(subprocess.check_output(command, cwd=root, text=True))
    components = []
    for package in sorted(
        payload.get("packages", []),
        key=lambda item: (item.get("name", ""), item.get("version", "")),
    ):
        if package.get("name") == "nebula-ui" or not package.get("source"):
            continue
        components.append(
            {
                "ecosystem": "cargo",
                "name": package["name"],
                "version": package["version"],
                "license": package.get("license") or "Not declared",
            }
        )
    if not components:
        raise RuntimeError("Cargo metadata contained no third-party packages")
    return components


def generate_notices(
    lockfile: Path, destination: Path, *, root: Path | None = None, target: str
) -> int:
    """Write deterministic license metadata for installed Core dependencies."""

    runtime = locked_main_packages(lockfile)
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    selected = [installed[name] for name in sorted(runtime & installed.keys())]
    if not selected:
        raise RuntimeError("no locked Nebula 3 dependencies are installed")

    root = (root or lockfile.resolve().parent).resolve()
    node = npm_components(root)
    rust = cargo_components(root, target)
    output = [
        "Nebula 3 Third-Party Notices",
        "================================",
        "",
        "This file is generated from the locked Python, npm, and Cargo dependency sets.",
        "Packages excluded by platform markers are not listed.",
        "",
    ]
    for distribution in selected:
        metadata = distribution.metadata
        name = metadata.get("Name", "unknown")
        version = distribution.version
        license_name = (
            metadata.get("License-Expression")
            or metadata.get("License")
            or "Not declared in package metadata"
        )
        output.extend(
            [
                f"{name} {version}",
                "-" * (len(name) + len(version) + 1),
                f"License: {license_name.strip()}",
            ]
        )
        project_urls = metadata.get_all("Project-URL") or []
        home_page = metadata.get("Home-page")
        if home_page:
            output.append(f"Home page: {home_page}")
        output.extend(f"Project URL: {value}" for value in project_urls)
        for license_file in _license_files(distribution):
            try:
                content = license_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeError(
                    f"cannot read license file for {name}: {license_file}"
                ) from exc
            output.extend(
                [
                    "",
                    f"Bundled license file: {license_file.name}",
                    "~" * (22 + len(license_file.name)),
                    content.rstrip(),
                ]
            )
        output.extend(["", ""])

    for heading, components in (
        ("Bundled web components", node),
        ("Bundled Rust components", rust),
    ):
        output.extend([heading, "=" * len(heading), ""])
        for component in components:
            output.append(
                f"{component['name']} {component['version']} — License: {component['license']}"
            )
        output.extend(["", ""])

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    return len(selected) + len(node) + len(rust)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lockfile", type=Path, default=Path("poetry.lock"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    count = generate_notices(
        arguments.lockfile,
        arguments.output,
        root=arguments.root,
        target=arguments.target,
    )
    print(f"wrote notices for {count} distributions to {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
