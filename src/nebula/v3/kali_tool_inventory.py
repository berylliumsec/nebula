"""Build the security-tool inventory embedded in Nebula's Kali workstation.

The module intentionally uses only the Python standard library so the same
source can be copied into the workstation image and executed during its build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping


if __package__:
    from .diagnostics import record_caught_exception
else:

    def record_caught_exception(
        feature: str,
        event_code: str,
        message: str,
        exception: BaseException,
        *,
        stage: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Skip host diagnostics when this file runs standalone in the image build."""


MANIFEST_SCHEMA = "nebula.kali-security-tools/v1"
TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}\Z")
PATH_DIRECTORIES = frozenset({"/bin", "/sbin", "/usr/bin", "/usr/sbin"})
REQUIRED_AUTOMATION_BINARIES = ("bash", "curl", "git", "python3", "rg")
_VERSION_ARGUMENTS = {
    "bash": ("--version",),
    "curl": ("--version",),
    "git": ("--version",),
    "python3": ("--version",),
    "rg": ("--version",),
}

# These are the non-security dependency groups in Kali's kali-linux-headless
# control stanza.  Keeping the boundary explicit prevents ubiquitous shell,
# editor, VCS, build, and service commands from becoming recorded by default.
# The image build still intersects this policy with the packages and binaries
# that are actually installed for its architecture.
EXCLUDED_DIRECT_PACKAGES = frozenset(
    {
        "7zip",
        "apache2",
        "atftpd",
        "axel",
        "bind9-dnsutils",
        "cifs-utils",
        "clang",
        "cryptsetup",
        "cryptsetup-initramfs",
        "cryptsetup-nuke-password",
        "default-mysql-server",
        "dos2unix",
        "ethtool",
        "expect",
        "gdisk",
        "git",
        "hashdeep",
        "hotpatch",
        "ifenslave",
        "iw",
        "kali-linux-core",
        "kali-system-cli",
        "libimage-exiftool-perl",
        "minicom",
        "miredo",
        "mlocate",
        "multimac",
        "netmask",
        "netsniff-ng",
        "ngrep",
        "openvpn",
        "php",
        "php-mysql",
        "pipx",
        "plocate",
        "powershell",
        "pwnat",
        "python3-pip",
        "python3-virtualenv",
        "rake",
        "rfkill",
        "sakis3g",
        "samba",
        "screen",
        "sendemail",
        "snmp",
        "snmpd",
        "socat",
        "sslh",
        "stunnel4",
        "swaks",
        "tcpick",
        "tcpreplay",
        "telnet",
        "testdisk",
        "tftp-hpa",
        "traceroute",
        "unar",
        "unrar",
        "upx-ucl",
        "vboot-kernel-utils",
        "vboot-utils",
        "vim",
        "vim-nox",
        "vlan",
        "vpnc",
        "whois",
        "xxd",
    }
)


def parse_status(document: str) -> dict[str, str]:
    """Return installed binary package names and their Depends fields."""

    packages: dict[str, str] = {}
    for paragraph in re.split(r"\n\s*\n", document):
        fields: dict[str, str] = {}
        current: str | None = None
        for line in paragraph.splitlines():
            if line[:1].isspace() and current is not None:
                fields[current] += " " + line.strip()
                continue
            if ":" not in line:
                current = None
                continue
            current, value = line.split(":", 1)
            fields[current] = value.strip()
        name = fields.get("Package")
        if name and fields.get("Status") == "install ok installed":
            packages[name] = fields.get("Depends", "")
    return packages


def package_versions(document: str) -> dict[str, str]:
    """Return installed package versions from the dpkg status document."""

    versions: dict[str, str] = {}
    for paragraph in re.split(r"\n\s*\n", document):
        name = re.search(r"^Package:\s*(\S+)\s*$", paragraph, re.MULTILINE)
        version = re.search(r"^Version:\s*(\S+)\s*$", paragraph, re.MULTILINE)
        status = re.search(r"^Status:\s*(.+)\s*$", paragraph, re.MULTILINE)
        if (
            name is not None
            and version is not None
            and status is not None
            and status.group(1) == "install ok installed"
        ):
            versions[name.group(1)] = version.group(1)
    return versions


def dependency_candidates(value: str) -> tuple[tuple[str, ...], ...]:
    """Parse the package-name portion of a Debian Depends field."""

    groups: list[tuple[str, ...]] = []
    for group in value.split(","):
        alternatives: list[str] = []
        for alternative in group.split("|"):
            candidate = re.sub(r"\([^)]*\)|\[[^]]*\]|<[^>]*>", "", alternative)
            candidate = (
                candidate.strip().split(maxsplit=1)[0] if candidate.strip() else ""
            )
            candidate = re.sub(r":(?:any|native|[A-Za-z0-9_-]+)\Z", "", candidate)
            if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", candidate):
                alternatives.append(candidate)
        if alternatives:
            groups.append(tuple(alternatives))
    return tuple(groups)


def security_packages(
    installed: dict[str, str],
    *,
    metapackage: str = "kali-linux-headless",
) -> tuple[str, ...]:
    """Resolve installed direct security dependencies of the metapackage."""

    depends = installed.get(metapackage)
    if depends is None:
        raise ValueError(f"installed package metadata is missing {metapackage}")
    resolved: set[str] = set()
    for alternatives in dependency_candidates(depends):
        selected = next((name for name in alternatives if name in installed), None)
        if selected is not None and selected not in EXCLUDED_DIRECT_PACKAGES:
            resolved.add(selected)
    return tuple(sorted(resolved))


def package_paths(package: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["dpkg-query", "-L", package],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return tuple(result.stdout.splitlines())


def executable_tools(
    packages: Iterable[str],
    *,
    paths_for_package=package_paths,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Return safe PATH executable basenames and their package provenance."""

    provenance: dict[str, set[str]] = {}
    for package in packages:
        for raw_path in paths_for_package(package):
            path = Path(raw_path)
            if str(path.parent) not in PATH_DIRECTORIES:
                continue
            name = path.name
            if TOOL_NAME_PATTERN.fullmatch(name) is None:
                continue
            try:
                executable = path.is_file() and os.access(path, os.X_OK)
            except OSError as caught_error:
                record_caught_exception(
                    "runtime",
                    "runtime.kali_tool_inventory.caught_failure_001",
                    "A handled runtime operation raised an exception.",
                    caught_error,
                    stage="kali_tool_inventory",
                )
                executable = False
            if executable:
                provenance.setdefault(name, set()).add(package)
    tools = tuple(sorted(provenance))
    return tools, {name: tuple(sorted(provenance[name])) for name in tools}


def build_manifest(
    status_document: str,
    *,
    paths_for_package=package_paths,
) -> dict[str, object]:
    installed = parse_status(status_document)
    packages = security_packages(installed)
    tools, provenance = executable_tools(
        packages,
        paths_for_package=paths_for_package,
    )
    if not tools:
        raise ValueError("Kali security-tool inventory is empty")
    versions = package_versions(status_document)
    binaries: list[dict[str, object]] = []
    for tool in tools:
        owners = provenance[tool]
        candidates = sorted(
            path
            for package in owners
            for path in paths_for_package(package)
            if Path(path).name == tool and str(Path(path).parent) in PATH_DIRECTORIES
        )
        if not candidates:
            raise ValueError(f"Kali security-tool path is missing for {tool}")
        binaries.append(
            {
                "name": tool,
                "path": candidates[0],
                "packages": list(owners),
                "versions": {
                    package: versions[package]
                    for package in owners
                    if package in versions
                },
            }
        )
    runtime_binaries: dict[str, dict[str, str]] = {}
    for item in binaries:
        name = str(item["name"])
        version_map = item["versions"]
        owner_packages = item["packages"]
        if not isinstance(version_map, dict) or not isinstance(owner_packages, list):
            raise ValueError(f"Kali security-tool provenance is invalid for {name}")
        version = ", ".join(
            f"{package}={package_version}"
            for package, package_version in sorted(version_map.items())
        ) or ",".join(str(package) for package in owner_packages)
        runtime_binaries[name] = {
            "name": name,
            "path": str(item["path"]),
            "version": version,
        }
    for name in REQUIRED_AUTOMATION_BINARIES:
        path = shutil.which(name)
        if path is None or not Path(path).is_absolute():
            raise ValueError(f"required automation binary is missing: {name}")
        result = subprocess.run(
            [path, *_VERSION_ARGUMENTS[name]],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        version = (
            result.stdout.splitlines()[0].strip() if result.stdout else "installed"
        )
        runtime_binaries[name] = {"name": name, "path": path, "version": version}
    return {
        "schema": MANIFEST_SCHEMA,
        "packages": list(packages),
        "tools": list(tools),
        "provenance": {
            name: list(package_names) for name, package_names in provenance.items()
        },
        "binaries": binaries,
        "runtime_binaries": [
            runtime_binaries[name] for name in sorted(runtime_binaries)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", default="/var/lib/dpkg/status")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    status = Path(arguments.status).read_text(encoding="utf-8")
    manifest = build_manifest(status)
    destination = Path(arguments.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o444)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
