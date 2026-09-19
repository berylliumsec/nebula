"""Discover and reach SSH environments listed in the Core host's ``~/.ssh/config``.

Operators already describe their research machines in ``~/.ssh/config``. This
module turns that file into a list of candidate environments without taking
ownership of it:

* the file is only read, never written; wildcard ``Host`` patterns and
  ``Match`` blocks are reported as skipped rather than listed;
* resolved connection facts come from ``ssh -G``, so the values shown are the
  ones the system client will actually use (``Include``, defaults, canonical
  names);
* connections always go through the system ``ssh`` binary with ``BatchMode``,
  so ControlMaster, ProxyJump, agents, and ``known_hosts`` keep working and no
  interactive prompt can hang Core. Private key contents are never read.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_CONFIG_PATH = Path("~/.ssh/config")
MAX_INCLUDE_DEPTH = 16
MAX_CONFIG_BYTES = 1_000_000
RESOLVE_TIMEOUT_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 10

_WILDCARD = re.compile(r"[*?!]")
_KEYWORD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*(?:=\s*|\s+)(.*?)\s*$")
# ``ssh -G`` keys that are useful to show an operator. Everything else in the
# resolved configuration stays out of API responses.
_RESOLVED_KEYS = {
    "hostname",
    "user",
    "port",
    "identityfile",
    "proxyjump",
    "hostkeyalias",
    "stricthostkeychecking",
    "controlmaster",
    "serveraliveinterval",
    "batchmode",
}

_DEFAULT_VALUES = {"", "none", "no", "false", "0"}
# Keys ``ssh -G`` lists when a host sets no IdentityFile of its own.
_DEFAULT_IDENTITY_NAMES = {
    "id_rsa",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_ed25519",
    "id_ed25519_sk",
    "id_xmss",
    "id_dsa",
}

ProbeStatus = Literal[
    "reachable",
    "unreachable",
    "auth_failed",
    "host_key_untrusted",
    "error",
]


@dataclass(frozen=True)
class SshConfigHost:
    """One concrete ``Host`` line. ``alias`` is its first concrete name."""

    alias: str
    aliases: tuple[str, ...]
    comment: str
    source: str
    line: int


@dataclass
class SshConfigScan:
    path: str
    exists: bool
    hosts: list[SshConfigHost] = field(default_factory=list)
    skipped_patterns: list[str] = field(default_factory=list)
    skipped_match_blocks: int = 0
    files_read: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedSshHost:
    alias: str
    hostname: str
    user: str
    port: int
    identity_files: tuple[str, ...]
    options: dict[str, str]


@dataclass(frozen=True)
class SshProbeResult:
    alias: str
    status: ProbeStatus
    latency_ms: int | None
    system: str = ""
    os_version: str = ""
    arch: str = ""
    model: str = ""
    tools: tuple[str, ...] = ()
    passwordless_sudo: bool | None = None
    detail: str = ""


def config_path(path: Path | str | None = None) -> Path:
    return Path(path or DEFAULT_CONFIG_PATH).expanduser()


def _split_args(value: str) -> list[str]:
    try:
        return shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value.split()


def _include_targets(value: str, base: Path) -> list[Path]:
    targets: list[Path] = []
    for pattern in _split_args(value):
        expanded = Path(pattern).expanduser()
        if not expanded.is_absolute():
            # Relative Include paths in a user config are relative to ~/.ssh.
            expanded = base / expanded
        targets.extend(Path(p) for p in sorted(glob.glob(str(expanded))))
    return targets


def discover_hosts(path: Path | str | None = None) -> SshConfigScan:
    """List concrete hosts from an ssh config file, following ``Include``."""

    root = config_path(path)
    scan = SshConfigScan(path=str(root), exists=root.is_file())
    if not scan.exists:
        return scan
    seen_aliases: set[str] = set()
    visited: set[Path] = set()
    base = root.parent

    def read(file: Path, depth: int) -> None:
        resolved = file.resolve()
        if resolved in visited or depth > MAX_INCLUDE_DEPTH:
            return
        visited.add(resolved)
        try:
            if file.stat().st_size > MAX_CONFIG_BYTES:
                scan.errors.append(
                    f"{file}: larger than {MAX_CONFIG_BYTES} bytes; skipped"
                )
                return
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            scan.errors.append(f"{file}: {exc.strerror or exc}")
            return
        scan.files_read.append(str(file))
        current: dict[str, object] | None = None
        # Only comments between a Host line and its first directive describe
        # that host; later ones usually introduce the next block.
        comments: list[str] = []
        collecting = False

        def flush() -> None:
            nonlocal current
            if current is not None:
                names = current["names"]
                assert isinstance(names, list)
                fresh = [name for name in names if name not in seen_aliases]
                if fresh:
                    seen_aliases.update(fresh)
                    scan.hosts.append(
                        SshConfigHost(
                            alias=fresh[0],
                            aliases=tuple(fresh),
                            comment=" ".join(comments).strip(),
                            source=str(file),
                            line=int(current["line"]),  # type: ignore[arg-type]
                        )
                    )
            current = None
            comments.clear()

        for number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if current is not None and collecting:
                    comments.append(stripped.lstrip("#").strip())
                continue
            match = _KEYWORD.match(raw)
            if not match:
                continue
            keyword, value = match.group(1).lower(), match.group(2)
            collecting = keyword == "host"
            if keyword == "host":
                flush()
                names: list[str] = []
                for name in _split_args(value):
                    if _WILDCARD.search(name):
                        if name not in scan.skipped_patterns:
                            scan.skipped_patterns.append(name)
                    else:
                        names.append(name)
                current = {"names": names, "line": number} if names else None
            elif keyword == "match":
                flush()
                scan.skipped_match_blocks += 1
            elif keyword == "include":
                flush()
                for target in _include_targets(value, base):
                    read(target, depth + 1)
        flush()

    read(root, 0)
    return scan


def ssh_binary() -> str | None:
    return shutil.which("ssh")


def _base_command(path: Path | str | None) -> list[str]:
    binary = ssh_binary()
    if binary is None:
        raise FileNotFoundError("The ssh client is not installed on the Core host.")
    command = [binary]
    root = config_path(path)
    if root != config_path(None):
        command += ["-F", str(root)]
    return command


def parse_resolved_config(alias: str, output: str) -> ResolvedSshHost:
    values: dict[str, list[str]] = {}
    for line in output.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        if key in _RESOLVED_KEYS and value:
            values.setdefault(key, []).append(value.strip())
    home = str(Path.home())

    def first(key: str, default: str = "") -> str:
        return values.get(key, [default])[0]

    identity_files = tuple(
        path.replace(home, "~", 1) if path.startswith(home) else path
        for path in values.get("identityfile", [])
    )
    if len(identity_files) > 1 and all(
        path.startswith("~/.ssh/") and path[len("~/.ssh/") :] in _DEFAULT_IDENTITY_NAMES
        for path in identity_files
    ):
        identity_files = ()
    options = {
        key: first(key)
        for key in sorted(_RESOLVED_KEYS - {"hostname", "user", "port", "identityfile"})
        if key in values and first(key).lower() not in _DEFAULT_VALUES
    }
    try:
        port = int(first("port", "22"))
    except ValueError:
        port = 22
    return ResolvedSshHost(
        alias=alias,
        hostname=first("hostname", alias),
        user=first("user"),
        port=port,
        identity_files=identity_files,
        options=options,
    )


def resolve_host(alias: str, path: Path | str | None = None) -> ResolvedSshHost:
    """Ask the system client what it would use for ``alias`` (no connection)."""

    _validate_alias(alias)
    completed = subprocess.run(
        [*_base_command(path), "-G", "--", alias],
        capture_output=True,
        text=True,
        timeout=RESOLVE_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or f"ssh -G {alias} failed")
    return parse_resolved_config(alias, completed.stdout)


def _validate_alias(alias: str) -> None:
    if (
        not alias
        or alias.startswith("-")
        or any(c.isspace() for c in alias)
        or _WILDCARD.search(alias)
    ):
        raise ValueError(f"Invalid SSH host alias: {alias!r}")


def remote_command_argv(
    alias: str,
    command: str,
    *,
    cwd: str | None = None,
    path: Path | str | None = None,
    connect_timeout: int = CONNECT_TIMEOUT_SECONDS,
) -> list[str]:
    """Build a non-interactive ``ssh`` invocation that runs ``command`` in ``/bin/sh``.

    The remote login shell may be zsh, fish, or anything else, so the command is
    handed to ``/bin/sh -c`` as one quoted word; ``cwd`` is entered first when
    given (a leading ``~`` is expanded by the remote shell).
    """

    _validate_alias(alias)
    script = command
    if cwd:
        target = cwd.strip()
        quoted = (
            '"$HOME"' + shlex.quote(target[1:])
            if target == "~" or target.startswith("~/")
            else shlex.quote(target)
        )
        script = f"cd {quoted} && {command}"
    return [
        *_base_command(path),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-T",
        "--",
        alias,
        f"exec /bin/sh -c {shlex.quote(script)}",
    ]


_PROBE_SCRIPT = r"""
echo "system=$(uname -s)"
echo "arch=$(uname -m)"
if command -v sw_vers >/dev/null 2>&1; then echo "os=macOS $(sw_vers -productVersion)";
elif [ -r /etc/os-release ]; then . /etc/os-release; echo "os=${PRETTY_NAME:-$NAME}"; fi
model=$(sysctl -n hw.model 2>/dev/null || tr -d '\000' </sys/firmware/devicetree/base/model 2>/dev/null)
echo "model=$model"
tools=""
for t in python3 git rsync docker xcrun gcc clang make; do command -v "$t" >/dev/null 2>&1 && tools="$tools $t"; done
echo "tools=$tools"
if sudo -n true >/dev/null 2>&1; then echo "sudo=yes"; else echo "sudo=no"; fi
""".strip()


def classify_ssh_failure(stderr: str) -> ProbeStatus:
    text = stderr.lower()
    if (
        "host key verification failed" in text
        or "remote host identification has changed" in text
    ):
        return "host_key_untrusted"
    if "permission denied" in text or "too many authentication failures" in text:
        return "auth_failed"
    if any(
        marker in text
        for marker in (
            "could not resolve hostname",
            "timed out",
            "no route to host",
            "connection refused",
            "network is unreachable",
            "connection closed",
        )
    ):
        return "unreachable"
    return "error"


def parse_probe_output(alias: str, output: str, latency_ms: int) -> SshProbeResult:
    facts: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"system", "arch", "os", "model", "tools", "sudo"}:
            facts[key] = value.strip()
    return SshProbeResult(
        alias=alias,
        status="reachable",
        latency_ms=latency_ms,
        system=facts.get("system", ""),
        os_version=facts.get("os", ""),
        arch=facts.get("arch", ""),
        model=facts.get("model", ""),
        tools=tuple(facts.get("tools", "").split()),
        passwordless_sudo=None if "sudo" not in facts else facts["sudo"] == "yes",
    )


def probe_host(alias: str, path: Path | str | None = None) -> SshProbeResult:
    """Connect once and fingerprint the host: OS, architecture, model, tools."""

    argv = remote_command_argv(alias, _PROBE_SCRIPT, path=path)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return SshProbeResult(
            alias=alias,
            status="unreachable",
            latency_ms=None,
            detail=f"No response within {int(PROBE_TIMEOUT_SECONDS)} seconds.",
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode == 255:
        stderr = completed.stderr.strip()
        return SshProbeResult(
            alias=alias,
            status=classify_ssh_failure(stderr),
            latency_ms=None,
            detail=stderr.splitlines()[-1] if stderr else "ssh exited with status 255.",
        )
    return parse_probe_output(alias, completed.stdout, latency_ms)
