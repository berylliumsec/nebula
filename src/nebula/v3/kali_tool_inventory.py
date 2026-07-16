"""Build the security-tool inventory embedded in Nebula's Kali workstation.

The module intentionally uses only the Python standard library so the same
source can be copied into the workstation image and executed during its build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
                    "toolbox",
                    "toolbox.kali_tool_inventory.caught_failure_001",
                    "A handled toolbox operation raised an exception.",
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
    return {
        "schema": MANIFEST_SCHEMA,
        "packages": list(packages),
        "tools": list(tools),
        "provenance": {
            name: list(package_names) for name, package_names in provenance.items()
        },
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
