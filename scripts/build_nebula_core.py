"""Build, audit, and place the target-triple Nebula Core sidecar for Tauri 2."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ``python scripts/build_nebula_core.py`` puts the scripts directory, rather
# than the repository root, on sys.path.  Keep direct invocation equivalent to
# the supported ``python -m scripts.build_nebula_core`` form used by CI.
if __package__ in {None, ""}:
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

from scripts.generate_third_party_notices import generate_notices
from scripts.nebula3_version import check_versions
from scripts.package_audit import FORBIDDEN_MODULES, inspect_pyinstaller_binary


def macos_codesign_arguments(
    *, platform: str, distribution: str, identity: str | None
) -> list[str]:
    """Return PyInstaller signing arguments for embedded Mach-O binaries."""

    if platform != "darwin":
        return []
    configured = (identity or "").strip()
    if distribution in {"direct", "managed"} and not configured:
        raise RuntimeError(
            "APPLE_SIGNING_IDENTITY is required before freezing a macOS release Core"
        )
    return ["--codesign-identity", configured] if configured else []


def target_triple() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise RuntimeError("rustc did not report a host target triple")


def git_commit(root: Path) -> str:
    configured = os.getenv("NEBULA_BUILD_COMMIT")
    if configured:
        return configured.strip()
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "NEBULA_BUILD_COMMIT is required outside a Git checkout"
        ) from exc


def write_build_metadata(
    destination: Path, *, version: str, target: str, root: Path
) -> dict[str, str]:
    timestamp = os.getenv("NEBULA_BUILD_TIMESTAMP") or datetime.now(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")
    metadata = {
        "version": version,
        "commit": git_commit(root),
        "target": os.getenv("NEBULA_BUILD_TARGET", target),
        "build_timestamp": timestamp,
        "distribution_channel": os.getenv("NEBULA_DISTRIBUTION_CHANNEL", "qa"),
    }
    if any(not value.strip() for value in metadata.values()):
        raise RuntimeError("Nebula build metadata values must not be empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return metadata


def stage_runtime_payload(root: Path) -> tuple[Path, Path]:
    """Create clean allowlisted data trees instead of freezing live build residue."""

    stage = root / "build" / "nebula-core-stage"
    shutil.rmtree(stage, ignore_errors=True)
    migrations = stage / "migrations"
    frontend = stage / "ui"
    migrations.mkdir(parents=True)
    frontend.mkdir(parents=True)

    migration_source = root / "src" / "nebula" / "v3" / "migrations"
    for source in sorted(migration_source.rglob("*")):
        if source.is_file() and source.suffix in {".py", ".mako"}:
            destination = migrations / source.relative_to(migration_source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    frontend_source = root / "ui" / "dist"
    if not (frontend_source / "index.html").is_file():
        raise RuntimeError("build ui/ before freezing Nebula Core")
    for source in sorted(frontend_source.rglob("*")):
        if source.is_file() and not source.name.endswith((".js.map", ".css.map")):
            destination = frontend / source.relative_to(frontend_source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return migrations, frontend


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = check_versions(root)
    migrations, frontend = stage_runtime_payload(root)
    license_file = root / "LICENSE.md"
    if not license_file.is_file():
        raise RuntimeError("LICENSE.md is required for a distributable Core")
    tool_pack_trust = (
        root / "src" / "nebula" / "v3" / "tool_pack_assets" / "trust"
    )
    if not (tool_pack_trust / "berylliumsec.json").is_file():
        raise RuntimeError("the embedded tool-pack trust root is required")
    report_assets = root / "src" / "nebula" / "v3" / "report_assets"
    required_fonts = (
        "NotoSans-Regular.ttf",
        "NotoSans-Bold.ttf",
        "NotoSansMono-Regular.ttf",
        "NotoSansMono-Bold.ttf",
        "OFL.txt",
    )
    if any(not (report_assets / "fonts" / name).is_file() for name in required_fonts):
        raise RuntimeError("the bundled report fonts and OFL license are required")
    operator_help = root / "src" / "nebula" / "v3" / "operator_help.md"
    if not operator_help.is_file():
        raise RuntimeError("the bundled Nebula 3 operator-help corpus is required")

    target = target_triple()
    metadata_root = root / "build" / "nebula-core-metadata"
    build_info = metadata_root / "BUILD_INFO.json"
    notices = metadata_root / "THIRD_PARTY_NOTICES.txt"
    identity = write_build_metadata(
        build_info, version=version, target=target, root=root
    )
    notice_count = generate_notices(
        root / "poetry.lock", notices, root=root, target=target
    )

    arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "nebula-core",
        "--specpath",
        str(root / "build" / "pyinstaller"),
        "--paths",
        str(root / "src"),
        "--collect-submodules",
        "nebula.v3",
        "--add-data",
        f"{migrations}:nebula/v3/migrations",
        "--add-data",
        f"{build_info}:nebula/v3",
        "--add-data",
        f"{frontend}:ui/dist",
        "--add-data",
        f"{license_file}:licenses",
        "--add-data",
        f"{notices}:licenses",
        "--add-data",
        f"{tool_pack_trust}:nebula/v3/tool_pack_assets/trust",
        "--add-data",
        f"{report_assets}:nebula/v3/report_assets",
        "--add-data",
        f"{operator_help}:nebula/v3",
    ]
    # Release environments omit the legacy dependency group. These exclusions
    # are a second defense for developer/QA builds created in a full checkout;
    # the mandatory post-build archive audit remains authoritative.
    for module in FORBIDDEN_MODULES:
        arguments.extend(["--exclude-module", module])
    arguments.extend(
        macos_codesign_arguments(
            platform=sys.platform,
            distribution=identity["distribution_channel"],
            identity=os.getenv("APPLE_SIGNING_IDENTITY"),
        )
    )
    arguments.append(str(root / "scripts" / "nebula_core_entry.py"))
    subprocess.run(arguments, cwd=root, check=True)

    suffix = ".exe" if sys.platform == "win32" else ""
    source = root / "dist" / f"nebula-core{suffix}"
    audit = inspect_pyinstaller_binary(source)
    destination = (
        root / "ui" / "src-tauri" / "binaries" / f"nebula-core-{target}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print(
        json.dumps(
            {
                "artifact": str(destination),
                "audit": audit,
                "build": identity,
                "third_party_distributions": notice_count,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
