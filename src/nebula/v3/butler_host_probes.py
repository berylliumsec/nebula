"""Butler supervisor read-only host probes, blocker checks and gated tmp cleanup.

``nebula.supervisor.probe`` runs one fixed, read-only command template per
probe kind (disk, memory, TCP, HTTP status, TLS chain, VPN configurations,
listening ports, lease state, host health). Every parameter is validated with
a strict pattern or integer range and quoted before it is formatted into the
template; there is no free-form shell. Network probes may name only lab hosts
or configured external hosts. ``kind="verify_blocker"`` maps a claimed blocker
(for example "no VPN configuration") to the probe that tests it and states
whether the probe output supports the claim.

``nebula.supervisor.tmp_cleanup`` finds idle full-repository exports under
operator-configured roots and deletes them only after a dry run: deletion
needs the dry-run token and a subset of its candidate paths, and idleness is
checked again at delete time.

Execution, guardrail screening and audit reuse ``butler_host_ops`` and
``butler_supervisor_policy``. Every call, allowed or refused, is audited.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .butler_host_ops import DEFAULT_TIMEOUT, ButlerHostOps, EventSink
from .butler_supervisor_policy import (
    GuardrailViolation,
    LabHost,
    SupervisorPolicy,
    digest,
    load_policy,
)
from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


LEASE_STATE_COMMAND_ENV = "NEBULA_BUTLER_SUPERVISOR_LEASE_STATE_COMMAND"
LEASE_STATE_HOST_ENV = "NEBULA_BUTLER_SUPERVISOR_LEASE_STATE_HOST"
HEALTH_PATHS_ENV = "NEBULA_BUTLER_SUPERVISOR_HEALTH_PATHS"
CLEANUP_ROOTS_ENV = "NEBULA_BUTLER_SUPERVISOR_CLEANUP_ROOTS"
CLEANUP_HOST_ENV = "NEBULA_BUTLER_SUPERVISOR_CLEANUP_HOST"
CLEANUP_MIN_BYTES_ENV = "NEBULA_BUTLER_SUPERVISOR_CLEANUP_MIN_BYTES"
CLEANUP_IDLE_MINUTES_ENV = "NEBULA_BUTLER_SUPERVISOR_CLEANUP_IDLE_MINUTES"

DEFAULT_CLEANUP_MIN_BYTES = 2 * 1024**3
DEFAULT_CLEANUP_IDLE_MINUTES = 20
CLEANUP_TOKEN_TTL_SECONDS = 15 * 60
_MAX_TOKENS = 64

PROBE_KINDS = (
    "disk_free",
    "mem_available",
    "tcp_reachable",
    "http_status",
    "tls_chain",
    "vpn_configs",
    "listening_ports",
    "lsof_port",
    "lease_state",
    "host_health",
    "verify_blocker",
)

# claim kind -> the probe that tests it
BLOCKER_PROBES = {
    "no_vpn_config": "vpn_configs",
    "tls_intercepted": "tls_chain",
    "disk_full": "disk_free",
    "host_unreachable": "tcp_reachable",
    "endpoint_unreachable": "http_status",
}

_PATH = re.compile(r"/[A-Za-z0-9._@+/-]{0,1000}")
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*"
)
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_URL = re.compile(
    r"(?P<scheme>https?)://(?P<host>[A-Za-z0-9.-]{1,253})(?::(?P<port>\d{1,5}))?"
    r"(?P<path>/[A-Za-z0-9._~%/+=&?,:@-]{0,1000})?"
)
_BROAD_ROOTS = frozenset(
    {
        "/",
        "/home",
        "/Users",
        "/root",
        "/srv",
        "/var",
        "/usr",
        "/etc",
        "/opt",
        "/bin",
        "/sbin",
        "/lib",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/System",
        "/Library",
        "/Applications",
        "/Volumes",
    }
)
_CHILD_NAME = re.compile(r"[A-Za-z0-9._@+-]{1,255}")


@dataclass(frozen=True)
class ProbeConfig:
    """Operator configuration for probes and cleanup (read on every call)."""

    lease_state_command: str | None = None
    lease_state_host: str | None = None
    health_paths: tuple[str, ...] = ("/",)
    cleanup_roots: tuple[str, ...] = ()
    cleanup_host: str | None = None
    cleanup_min_bytes: int = DEFAULT_CLEANUP_MIN_BYTES
    cleanup_idle_minutes: int = DEFAULT_CLEANUP_IDLE_MINUTES


def _commas(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def load_probe_config(environ: Mapping[str, str] | None = None) -> ProbeConfig:
    env = os.environ if environ is None else environ
    return ProbeConfig(
        lease_state_command=(env.get(LEASE_STATE_COMMAND_ENV) or "").strip() or None,
        lease_state_host=(env.get(LEASE_STATE_HOST_ENV) or "").strip() or None,
        health_paths=_commas(env.get(HEALTH_PATHS_ENV)) or ("/",),
        cleanup_roots=_commas(env.get(CLEANUP_ROOTS_ENV)),
        cleanup_host=(env.get(CLEANUP_HOST_ENV) or "").strip() or None,
        cleanup_min_bytes=int(
            env.get(CLEANUP_MIN_BYTES_ENV) or DEFAULT_CLEANUP_MIN_BYTES
        ),
        cleanup_idle_minutes=int(
            env.get(CLEANUP_IDLE_MINUTES_ENV) or DEFAULT_CLEANUP_IDLE_MINUTES
        ),
    )


class _AuditedRefusal(GuardrailViolation):
    """A refusal that has already been written to the audit log."""


@dataclass
class _CleanupScan:
    host: str
    created: float
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)


_SCANS: dict[str, _CleanupScan] = {}
_SCANS_LOCK = threading.Lock()


def _invalid(detail: str) -> GuardrailViolation:
    return GuardrailViolation("invalid_argument", detail)


def _int(
    arguments: Mapping[str, Any],
    key: str,
    low: int,
    high: int,
    default: int | None = None,
) -> int:
    value = arguments.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise _invalid(f"{key} must be an integer in [{low}, {high}]")
    return value


def _safe_path(value: Any, key: str = "path") -> str:
    if not isinstance(value, str) or not _PATH.fullmatch(value):
        raise _invalid(f"{key} must be an absolute path of [A-Za-z0-9._@+/-]")
    if any(part == ".." for part in value.split("/")):
        raise _invalid(f"{key} may not contain '..'")
    return value.rstrip("/") or "/"


def _within(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root.rstrip("/") + "/")


def _touches_protected(policy: SupervisorPolicy, path: str) -> bool:
    for protected in policy.protected_paths:
        root = protected.rstrip("/") or "/"
        if _within(path, root) or _within(root, path):
            return True
    return False


# --------------------------------------------------------------------------
# Output parsers (pure; unit-tested directly)
# --------------------------------------------------------------------------

_DF_LINE = re.compile(
    r"^(?P<fs>.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(?P<mount>/.*)$"
)


def parse_df(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        match = _DF_LINE.match(line.strip())
        if not match:
            continue
        total, used, avail, percent = (int(match.group(i)) for i in (2, 3, 4, 5))
        rows.append(
            {
                "filesystem": match.group("fs"),
                "mount": match.group("mount"),
                "total_bytes": total * 1024,
                "used_bytes": used * 1024,
                "free_bytes": avail * 1024,
                "percent_used": percent,
            }
        )
    return rows


def parse_mem(text: str) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(MemTotal|MemAvailable):\s+(\d+)\s*kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    if "MemAvailable" in values:
        return {
            "method": "proc_meminfo",
            "available_bytes": values["MemAvailable"],
            "total_bytes": values.get("MemTotal"),
        }
    total = re.search(r"hw\.memsize:\s*(\d+)", text)
    page = re.search(r"page size of (\d+) bytes", text)
    if page:
        pages = 0
        for label in ("free", "inactive", "speculative", "purgeable"):
            found = re.search(rf"Pages {label}:\s+(\d+)\.", text)
            if found:
                pages += int(found.group(1))
        return {
            "method": "vm_stat_free_inactive_speculative_purgeable",
            "available_bytes": pages * int(page.group(1)),
            "total_bytes": int(total.group(1)) if total else None,
        }
    return {"method": None, "available_bytes": None, "total_bytes": None}


_VPN_LINE = re.compile(
    r"^\*?\s*\((?P<status>[^)]*)\)\s+(?P<id>[0-9A-Fa-f-]{36})\s+(?P<type>.*?)\s+"
    r"\"(?P<name>.*)\"\s*(?P<kind>\[[^\]]*\])?\s*$"
)


def parse_scutil_nc(text: str) -> list[dict[str, Any]]:
    configs = []
    for line in text.splitlines():
        match = _VPN_LINE.match(line.strip())
        if match:
            configs.append(
                {
                    "status": match.group("status"),
                    "id": match.group("id"),
                    "type": match.group("type").strip(),
                    "name": match.group("name"),
                    "kind": (match.group("kind") or "").strip("[]") or None,
                }
            )
    return configs


def parse_lsof_listen(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 9 or parts[0] == "COMMAND" or not parts[1].isdigit():
            continue
        name = parts[-2] if parts[-1].startswith("(") else parts[-1]
        port = name.rsplit(":", 1)[-1]
        rows.append(
            {
                "command": parts[0],
                "pid": int(parts[1]),
                "user": parts[2],
                "address": name,
                "port": int(port) if port.isdigit() else None,
                "state": parts[-1].strip("()") if parts[-1].startswith("(") else None,
            }
        )
    return rows


def parse_tls_chain(text: str) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    for line in text.splitlines():
        subject = re.match(r"^\s*(\d+)\s+s:(.*)$", line)
        if subject:
            chain.append(
                {
                    "depth": int(subject.group(1)),
                    "subject": subject.group(2).strip(),
                    "issuer": None,
                }
            )
            continue
        issuer = re.match(r"^\s+i:(.*)$", line)
        if issuer and chain and chain[-1]["issuer"] is None:
            chain[-1]["issuer"] = issuer.group(1).strip()
    verify = re.search(r"Verify return code:\s*(\d+)\s*\(([^)]*)\)", text)
    return {
        "chain": chain,
        "verify_code": int(verify.group(1)) if verify else None,
        "verify_text": verify.group(2) if verify else None,
        "handshake": bool(chain),
    }


def _parse_scan(text: str) -> tuple[str | None, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line in {"ROOT_MISSING", "ROOT_NOT_CANONICAL"}:
            return line.lower(), []
        parts = line.split("\t")
        if len(parts) != 6 or parts[0] != "CAND":
            continue
        _, has_git, kb, recent, held, path = parts
        rows.append(
            {
                "path": path,
                "has_git": has_git == "1",
                "bytes": int(kb) * 1024 if kb.isdigit() else None,
                "recent": {"1": True, "0": False}.get(recent),
                "held_open": {"1": True, "0": False}.get(held),
            }
        )
    return None, rows


def _scan_script(root: str, idle_minutes: int, min_kb: int, only: str | None) -> str:
    """POSIX sh (also zsh) scan of the direct children of one root.

    Prints ``CAND\\t<git>\\t<kb>\\t<recent>\\t<held>\\t<path>``; ``recent`` and
    ``held`` are ``1``, ``0`` or ``x`` (check could not run). ``find`` never
    follows symlinks; ``du -P`` neither.
    """

    r = shlex.quote(root)
    source = (
        f"printf '%s\\n' {shlex.quote(only)}"
        if only
        else 'find "$r" -mindepth 1 -maxdepth 1 -type d'
    )
    return (
        f"r={r}; "
        '{ [ -d "$r" ] && [ ! -L "$r" ]; } || { echo ROOT_MISSING; exit 0; }; '
        '[ "$(cd "$r" && pwd -P)" = "$r" ] || { echo ROOT_NOT_CANONICAL; exit 0; }; '
        f"{source} | while IFS= read -r d; do "
        '[ -L "$d" ] && continue; [ -d "$d" ] || continue; '
        'g=0; { [ -d "$d/.git" ] && [ ! -L "$d/.git" ]; } && g=1; '
        's=$(du -skP "$d" 2>/dev/null | cut -f1); [ -n "$s" ] || s=x; '
        f'f=$(find "$d" -mmin -{idle_minutes} -print -quit 2>/dev/null); fe=$?; '
        'if [ -n "$f" ]; then rc=1; elif [ $fe -eq 0 ]; then rc=0; else rc=x; fi; '
        "o=x; "
        f'if [ $rc = 0 ] && {{ [ $g = 1 ] || {{ [ "$s" != x ] && [ "$s" -ge {min_kb} ]; }}; }} '
        "&& command -v lsof >/dev/null 2>&1; then "
        'h=$(lsof -w -t +D "$d" 2>&1); c=$?; '
        # lsof exits 1 both when nothing is open and (with +D) when PIDs are
        # listed, so judge by the output: only PIDs -> held; nothing -> idle.
        'if [ -n "$h" ]; then '
        "if [ -z \"$(printf '%s' \"$h\" | tr -d '0123456789\\n')\" ]; then o=1; fi; "
        "elif [ $c -le 1 ]; then o=0; fi; "
        "fi; "
        'printf \'CAND\\t%s\\t%s\\t%s\\t%s\\t%s\\n\' "$g" "$s" "$rc" "$o" "$d"; '
        "done"
    )


class ButlerHostProbes:
    """Read-only probes, blocker verification and gated tmp cleanup for Butler."""

    def __init__(
        self,
        *,
        project_id: str,
        actor: str,
        policy_loader: Callable[[], SupervisorPolicy] = load_policy,
        config_loader: Callable[[], ProbeConfig] = load_probe_config,
        event_sink: EventSink | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ops = ButlerHostOps(
            project_id=project_id,
            actor=actor,
            policy_loader=policy_loader,
            event_sink=event_sink,
        )
        self._config_loader = config_loader
        self._clock = clock

    # ---- shared helpers --------------------------------------------------

    async def _refuse(
        self,
        policy: SupervisorPolicy,
        tool: str,
        error: GuardrailViolation,
        **fields: Any,
    ) -> None:
        try:
            await self._ops._refuse(policy, tool, error, **fields)
        except GuardrailViolation as raised:
            raise _AuditedRefusal(raised.rule, raised.detail) from raised

    async def _run(
        self,
        policy: SupervisorPolicy,
        tool: str,
        host: LabHost,
        command: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        reason: str,
    ) -> dict[str, Any]:
        try:
            return await self._ops._run_on(
                policy,
                tool=tool,
                host=host,
                command=command,
                timeout=timeout,
                cwd=None,
                reason=reason,
            )
        except GuardrailViolation as error:
            # _run_on has already audited the screening refusal.
            raise _AuditedRefusal(error.rule, error.detail) from error

    @staticmethod
    def _local(policy: SupervisorPolicy) -> LabHost:
        local = next((h for h in policy.hosts.values() if h.local), None)
        if local is None:
            raise GuardrailViolation(
                "host_not_allowlisted", "no local lab host configured"
            )
        return local

    def _host(self, policy: SupervisorPolicy, name: Any) -> LabHost:
        if name is None:
            return self._local(policy)
        if not isinstance(name, str):
            raise _invalid("host must be a string")
        return policy.host(name)

    @staticmethod
    def _target_address(policy: SupervisorPolicy, target: Any, runner: LabHost) -> str:
        """Resolve a lab host name or lab address. Never a non-lab address."""

        if not isinstance(target, str) or not (
            _HOSTNAME.fullmatch(target) or _IPV4.fullmatch(target)
        ):
            raise _invalid("target must be a lab host name or address")
        host = policy.hosts.get(target)
        if host is not None:
            if host.address:
                return host.address
            if host.local and runner.local:
                return "127.0.0.1"
            raise GuardrailViolation(
                "invalid_argument",
                f"lab host {target!r} has no address reachable from {runner.name}",
            )
        if target.casefold() in policy.lab_addresses():
            return target
        raise GuardrailViolation(
            "host_not_allowlisted", f"{target!r} is not a lab host or lab address"
        )

    @staticmethod
    def _network_name_allowed(policy: SupervisorPolicy, name: str) -> bool:
        folded = name.casefold()
        if folded in policy.lab_addresses():
            return True
        external = {item.casefold() for item in policy.external_hosts}
        return folded in external or any(
            folded.endswith("." + item) for item in external
        )

    @staticmethod
    def _raw(result: dict[str, Any]) -> dict[str, Any]:
        return {
            key: result.get(key)
            for key in ("host", "exit_code", "timed_out", "stdout", "stderr", "audit")
        }

    # ---- probes ----------------------------------------------------------

    async def _disk_free(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        host = self._host(policy, args.get("host"))
        path = _safe_path(args.get("path", "/"))
        result = await self._run(
            policy,
            "probe.disk_free",
            host,
            f"df -Pk -- {shlex.quote(path)}",
            timeout=60,
            reason=reason,
        )
        rows = parse_df(result["stdout"])
        return {
            "kind": "disk_free",
            "path": path,
            "disk": rows[0] if rows else None,
            "raw": self._raw(result),
        }

    async def _mem_available(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        host = self._host(policy, args.get("host"))
        command = (
            "if [ -r /proc/meminfo ]; then grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; "
            'else echo "hw.memsize: $(sysctl -n hw.memsize)"; vm_stat; fi'
        )
        result = await self._run(
            policy, "probe.mem_available", host, command, timeout=60, reason=reason
        )
        return {
            "kind": "mem_available",
            "memory": parse_mem(result["stdout"]),
            "raw": self._raw(result),
        }

    async def _tcp_reachable(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        runner = self._host(policy, args.get("host"))
        address = self._target_address(policy, args.get("target"), runner)
        port = _int(args, "port", 1, 65_535)
        wait = _int(args, "timeout_seconds", 1, 30, 5)
        command = f"nc -z -w {wait} {shlex.quote(address)} {port}"
        result = await self._run(
            policy,
            "probe.tcp_reachable",
            runner,
            command,
            timeout=wait + 15,
            reason=reason,
        )
        code = result["exit_code"]
        reachable = None if result["timed_out"] or code == 127 else code == 0
        return {
            "kind": "tcp_reachable",
            "from": runner.name,
            "target": address,
            "port": port,
            "reachable": reachable,
            "raw": self._raw(result),
        }

    async def _http_status(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        runner = self._host(policy, args.get("host"))
        url = args.get("url")
        match = _URL.fullmatch(url) if isinstance(url, str) else None
        if match is None:
            raise _invalid(
                "url must be http(s)://host[:port][/path] with safe characters"
            )
        if not self._network_name_allowed(policy, match.group("host")):
            raise GuardrailViolation(
                "host_not_allowlisted",
                f"{match.group('host')!r} is not a lab host or configured external host",
            )
        method = args.get("method", "HEAD")
        if method not in {"HEAD", "GET"}:
            raise _invalid("method must be HEAD or GET")
        wait = _int(args, "timeout_seconds", 1, 30, 10)
        head = " -I" if method == "HEAD" else ""
        command = f"curl -sS -o /dev/null -w '%{{http_code}}' -m {wait}{head} {shlex.quote(match.group(0))}"
        result = await self._run(
            policy,
            "probe.http_status",
            runner,
            command,
            timeout=wait + 15,
            reason=reason,
        )
        text = result["stdout"].strip()
        status = int(text) if text.isdigit() else None
        return {
            "kind": "http_status",
            "url": url,
            "method": method,
            "status_code": status,
            "curl_exit_code": result["exit_code"],
            "raw": self._raw(result),
        }

    async def _tls_chain(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        runner = self._host(policy, args.get("host"))
        target = args.get("target")
        if not isinstance(target, str) or not (
            _HOSTNAME.fullmatch(target) or _IPV4.fullmatch(target)
        ):
            raise _invalid("target must be a hostname or IPv4 address")
        if target in policy.hosts:
            target = self._target_address(policy, target, runner)
        elif not self._network_name_allowed(policy, target):
            raise GuardrailViolation(
                "host_not_allowlisted",
                f"{target!r} is not a lab host or configured external host",
            )
        port = _int(args, "port", 1, 65_535, 443)
        servername = args.get("servername", target)
        if not isinstance(servername, str) or not _HOSTNAME.fullmatch(servername):
            raise _invalid("servername must be a hostname")
        command = (
            f"openssl s_client -connect {shlex.quote(target)}:{port} "
            f"-servername {shlex.quote(servername)} -showcerts </dev/null 2>&1"
        )
        result = await self._run(
            policy, "probe.tls_chain", runner, command, timeout=45, reason=reason
        )
        parsed = parse_tls_chain(result["stdout"])
        # The certificates themselves are long; the parsed chain carries the facts.
        raw = self._raw(result)
        raw["stdout"] = "\n".join(
            line
            for line in result["stdout"].splitlines()
            if not re.fullmatch(r"[A-Za-z0-9+/=]{40,}", line)
        )
        return {
            "kind": "tls_chain",
            "target": target,
            "port": port,
            "servername": servername,
            **parsed,
            "raw": raw,
        }

    async def _vpn_configs(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        host = self._host(policy, args.get("host"))
        result = await self._run(
            policy,
            "probe.vpn_configs",
            host,
            "scutil --nc list",
            timeout=30,
            reason=reason,
        )
        return {
            "kind": "vpn_configs",
            "ok": result["exit_code"] == 0,
            "configs": parse_scutil_nc(result["stdout"]),
            "raw": self._raw(result),
        }

    async def _listening(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str, kind: str
    ) -> dict[str, Any]:
        host = self._host(policy, args.get("host"))
        if kind == "lsof_port" or args.get("port") is not None:
            port: int | None = _int(args, "port", 1, 65_535)
            selector = f"-iTCP:{port}"
        else:
            port = None
            selector = "-iTCP"
        command = f"lsof -nP {selector} -sTCP:LISTEN"
        result = await self._run(
            policy, f"probe.{kind}", host, command, timeout=60, reason=reason
        )
        rows = parse_lsof_listen(result["stdout"])
        # lsof exits 1 with no output when nothing matches.
        ok = result["exit_code"] == 0 or (
            result["exit_code"] == 1 and not result["stdout"].strip()
        )
        return {
            "kind": kind,
            "port": port,
            "ok": ok,
            "listening": bool(rows) if ok else None,
            "listeners": rows,
            "raw": self._raw(result),
        }

    async def _lease_state(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        config = self._config_loader()
        if not config.lease_state_command:
            raise GuardrailViolation(
                "not_configured",
                f"set {LEASE_STATE_COMMAND_ENV} to a read-only lease state command",
            )
        host = self._host(policy, config.lease_state_host)
        result = await self._run(
            policy,
            "probe.lease_state",
            host,
            config.lease_state_command,
            timeout=60,
            reason=reason,
        )
        parsed: Any = None
        try:
            parsed = json.loads(result["stdout"])
        except (
            ValueError
        ):  # diagnostic-expected: non-JSON lease output is returned raw.
            parsed = None
        return {
            "kind": "lease_state",
            "ok": result["exit_code"] == 0,
            "state": parsed,
            "raw": self._raw(result),
        }

    async def _host_health(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        host = self._host(policy, args.get("host"))
        config = self._config_loader()
        disks = []
        for path in config.health_paths:
            disks.append(
                await self._disk_free(policy, {"host": host.name, "path": path}, reason)
            )
        memory = await self._mem_available(policy, {"host": host.name}, reason)
        return {
            "kind": "host_health",
            "host": host.name,
            "disks": [
                {
                    "path": d["path"],
                    "disk": d["disk"],
                    "exit_code": d["raw"]["exit_code"],
                }
                for d in disks
            ],
            "memory": memory["memory"],
            "raw": {"disks": [d["raw"] for d in disks], "memory": memory["raw"]},
        }

    async def _verify_blocker(
        self, policy: SupervisorPolicy, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        claim = args.get("claim_kind")
        if claim not in BLOCKER_PROBES:
            raise _invalid(f"claim_kind must be one of {sorted(BLOCKER_PROBES)}")
        probe = BLOCKER_PROBES[claim]
        evidence = await self._dispatch(policy, probe, args, reason)
        supported: bool | None
        basis: str
        if claim == "no_vpn_config":
            wanted = args.get("name_substring")
            if not evidence["ok"]:
                supported, basis = None, "scutil --nc list failed"
            elif isinstance(wanted, str) and wanted:
                names = [c["name"] for c in evidence["configs"]]
                supported = not any(wanted.casefold() in n.casefold() for n in names)
                basis = f"configured VPN names checked for {wanted!r}"
            else:
                supported = not evidence["configs"]
                basis = f"{len(evidence['configs'])} VPN configuration(s) listed"
        elif claim == "tls_intercepted":
            expected = args.get("expected_issuer_substring")
            if not evidence["handshake"]:
                supported, basis = (
                    None,
                    "no TLS handshake; interception cannot be judged",
                )
            elif isinstance(expected, str) and expected:
                issuers = " | ".join(str(c.get("issuer")) for c in evidence["chain"])
                supported = expected.casefold() not in issuers.casefold()
                basis = f"chain issuers checked for {expected!r}"
            elif evidence["verify_code"] is None:
                supported, basis = None, "no verify result"
            else:
                supported = evidence["verify_code"] != 0
                basis = (
                    "chain verification against the host trust store; an interceptor "
                    "trusted by the host would still verify"
                )
        elif claim == "disk_full":
            disk = evidence["disk"]
            min_free = _int(args, "min_free_bytes", 0, 2**62, 1024**3)
            if disk is None:
                supported, basis = None, "df gave no parseable row"
            else:
                supported = disk["free_bytes"] < min_free or disk["percent_used"] >= 98
                basis = f"free {disk['free_bytes']} bytes vs minimum {min_free}; {disk['percent_used']}% used"
        elif claim == "host_unreachable":
            reachable = evidence["reachable"]
            supported = None if reachable is None else not reachable
            basis = "nc -z TCP connect result"
        else:  # endpoint_unreachable
            status = evidence["status_code"]
            if status is None:
                supported, basis = None, "curl gave no status code"
            else:
                supported = status == 0
                basis = f"HTTP status {status:03d} (000 means no HTTP response)"
        return {
            "kind": "verify_blocker",
            "claim_kind": claim,
            "probe": probe,
            "claim_supported": supported,
            "basis": basis,
            "evidence": evidence,
        }

    async def _dispatch(
        self, policy: SupervisorPolicy, kind: str, args: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        if kind == "disk_free":
            return await self._disk_free(policy, args, reason)
        if kind == "mem_available":
            return await self._mem_available(policy, args, reason)
        if kind == "tcp_reachable":
            return await self._tcp_reachable(policy, args, reason)
        if kind == "http_status":
            return await self._http_status(policy, args, reason)
        if kind == "tls_chain":
            return await self._tls_chain(policy, args, reason)
        if kind == "vpn_configs":
            return await self._vpn_configs(policy, args, reason)
        if kind in {"listening_ports", "lsof_port"}:
            return await self._listening(policy, args, reason, kind)
        if kind == "lease_state":
            return await self._lease_state(policy, args, reason)
        if kind == "host_health":
            return await self._host_health(policy, args, reason)
        if kind == "verify_blocker":
            return await self._verify_blocker(policy, args, reason)
        raise _invalid(f"kind must be one of {list(PROBE_KINDS)}")

    async def probe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._ops._policy()
        kind = str(arguments.get("kind"))
        reason = str(arguments.get("reason") or f"read-only probe {kind}")
        detail = json.dumps(arguments, sort_keys=True, default=str)
        try:
            result = await self._dispatch(policy, kind, arguments, reason)
        except _AuditedRefusal:
            raise
        except GuardrailViolation as error:
            await self._refuse(policy, "probe", error, kind=kind, command=detail)
            raise  # unreachable: _refuse raises
        summary = {
            key: result.get(key)
            for key in (
                "claim_kind",
                "claim_supported",
                "reachable",
                "status_code",
                "listening",
            )
            if key in result
        }
        await self._ops._record(
            policy,
            {
                "tool": "probe",
                "kind": kind,
                "outcome": "completed",
                "reason": reason,
                "detail": json.dumps(
                    {"arguments": arguments, "summary": summary},
                    sort_keys=True,
                    default=str,
                ),
            },
        )
        return result

    # ---- tmp cleanup -----------------------------------------------------

    def _cleanup_roots(
        self, policy: SupervisorPolicy, config: ProbeConfig
    ) -> list[str]:
        if not config.cleanup_roots:
            raise GuardrailViolation(
                "not_configured",
                f"set {CLEANUP_ROOTS_ENV} to the tmp roots Butler may clean",
            )
        roots = []
        for raw in config.cleanup_roots:
            root = _safe_path(raw, CLEANUP_ROOTS_ENV)
            if root in _BROAD_ROOTS:
                raise GuardrailViolation(
                    "cleanup_root_refused", f"{root} is too broad a cleanup root"
                )
            if _touches_protected(policy, root):
                raise GuardrailViolation(
                    "production_mirror_read_only", f"{root} overlaps a protected path"
                )
            roots.append(root)
        return roots

    def _classify(
        self,
        policy: SupervisorPolicy,
        config: ProbeConfig,
        roots: list[str],
        row: dict[str, Any],
    ) -> str | None:
        """Return why a scanned directory is not a deletion candidate, or None."""

        path = row["path"]
        parent, _, name = path.rpartition("/")
        if (
            parent not in roots
            or not _CHILD_NAME.fullmatch(name)
            or name in {".", ".."}
        ):
            return "unsafe_name_or_outside_root"
        if _touches_protected(policy, path):
            return "protected_path"
        if not (
            row["has_git"]
            or (row["bytes"] is not None and row["bytes"] >= config.cleanup_min_bytes)
        ):
            return "not_a_repository_export"
        if row["recent"] is None:
            return "idle_check_failed"
        if row["recent"]:
            return f"modified_within_{config.cleanup_idle_minutes}_minutes"
        if row["held_open"] is None:
            return "open_file_check_unavailable"
        if row["held_open"]:
            return "held_open_by_a_process"
        return None

    async def _scan(
        self,
        policy: SupervisorPolicy,
        config: ProbeConfig,
        host: LabHost,
        root: str,
        reason: str,
        timeout: int,
        only: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]], dict[str, Any]]:
        min_kb = max(1, config.cleanup_min_bytes // 1024)
        script = _scan_script(root, config.cleanup_idle_minutes, min_kb, only)
        result = await self._run(
            policy, "tmp_cleanup.scan", host, script, timeout=timeout, reason=reason
        )
        if result["exit_code"] != 0 or result["timed_out"]:
            return "scan_failed", [], self._raw(result)
        status, rows = _parse_scan(result["stdout"])
        return status, rows, self._raw(result)

    async def tmp_cleanup(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._ops._policy()
        reason = str(arguments.get("reason") or "tmp cleanup")
        dry_run = arguments.get("dry_run", True) is not False
        try:
            config = self._config_loader()
            roots = self._cleanup_roots(policy, config)
            host = self._host(policy, config.cleanup_host)
            timeout = _int(arguments, "timeout_seconds", 10, 1_800, 600)
            if dry_run:
                return await self._cleanup_dry_run(
                    policy, config, host, roots, reason, timeout
                )
            token = arguments.get("token")
            paths = arguments.get("paths")
            if not isinstance(token, str) or not isinstance(paths, list) or not paths:
                raise _invalid(
                    "deletion needs dry_run=false, the dry-run token and a non-empty paths list"
                )
            with _SCANS_LOCK:
                scan = _SCANS.get(token)
            if scan is None or self._clock() - scan.created > CLEANUP_TOKEN_TTL_SECONDS:
                raise GuardrailViolation(
                    "cleanup_token_expired",
                    "run a fresh dry run; tokens last 15 minutes",
                )
            if scan.host != host.name:
                raise GuardrailViolation(
                    "cleanup_token_mismatch", "token was issued for another host"
                )
            unknown = [
                p for p in paths if not isinstance(p, str) or p not in scan.candidates
            ]
            if unknown:
                raise GuardrailViolation(
                    "cleanup_path_not_candidate",
                    f"not in the dry-run candidates: {unknown}",
                )
        except _AuditedRefusal:
            raise
        except GuardrailViolation as error:
            await self._refuse(
                policy,
                "tmp_cleanup",
                error,
                command=json.dumps(arguments, sort_keys=True, default=str),
            )
            raise  # unreachable: _refuse raises
        return await self._cleanup_delete(
            policy, config, host, roots, scan, token, paths, reason, timeout
        )

    async def _cleanup_dry_run(
        self,
        policy: SupervisorPolicy,
        config: ProbeConfig,
        host: LabHost,
        roots: list[str],
        reason: str,
        timeout: int,
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        root_status: dict[str, str] = {}
        for root in roots:
            status, rows, _raw = await self._scan(
                policy, config, host, root, reason, timeout
            )
            root_status[root] = status or "scanned"
            for row in rows:
                why = self._classify(policy, config, roots, row)
                (skipped if why else candidates).append(
                    {**row, **({"skip_reason": why} if why else {})}
                )
        token = secrets.token_urlsafe(16)
        now = self._clock()
        with _SCANS_LOCK:
            for key in [
                k
                for k, v in _SCANS.items()
                if now - v.created > CLEANUP_TOKEN_TTL_SECONDS
            ]:
                del _SCANS[key]
            while len(_SCANS) >= _MAX_TOKENS:
                del _SCANS[min(_SCANS, key=lambda k: _SCANS[k].created)]
            _SCANS[token] = _CleanupScan(
                host=host.name,
                created=now,
                candidates={c["path"]: c for c in candidates},
            )
        await self._ops._record(
            policy,
            {
                "tool": "tmp_cleanup",
                "outcome": "dry_run",
                "host": host.name,
                "reason": reason,
                "token_sha256": _digest_token(token),
                "detail": json.dumps(
                    {
                        "roots": root_status,
                        "candidates": [(c["path"], c["bytes"]) for c in candidates],
                    },
                    default=str,
                ),
            },
        )
        return {
            "dry_run": True,
            "host": host.name,
            "token": token,
            "expires_in_seconds": CLEANUP_TOKEN_TTL_SECONDS,
            "idle_minutes": config.cleanup_idle_minutes,
            "min_bytes": config.cleanup_min_bytes,
            "roots": root_status,
            "candidates": candidates,
            "total_bytes": sum(c["bytes"] or 0 for c in candidates),
            "skipped": skipped,
        }

    async def _cleanup_delete(
        self,
        policy: SupervisorPolicy,
        config: ProbeConfig,
        host: LabHost,
        roots: list[str],
        scan: _CleanupScan,
        token: str,
        paths: list[str],
        reason: str,
        timeout: int,
    ) -> dict[str, Any]:
        deleted: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for path in paths:
            root = path.rpartition("/")[0]
            if root not in roots:
                kept.append({"path": path, "reason": "root_no_longer_configured"})
                continue
            status, rows, _raw = await self._scan(
                policy, config, host, root, reason, timeout, only=path
            )
            row = rows[0] if rows and rows[0]["path"] == path else None
            why = status or (
                self._classify(policy, config, roots, row)
                if row
                else "gone_or_not_a_directory"
            )
            if why:
                kept.append({"path": path, "reason": why})
                await self._ops._record(
                    policy,
                    {
                        "tool": "tmp_cleanup",
                        "outcome": "kept",
                        "host": host.name,
                        "detail": f"{path}: {why}",
                    },
                )
                continue
            assert row is not None
            quoted = shlex.quote(path)
            command = (
                f"p={quoted}; "
                '{ [ -d "$p" ] && [ ! -L "$p" ]; } || exit 7; '
                f'f=$(find "$p" -mmin -{config.cleanup_idle_minutes} -print -quit 2>/dev/null) || exit 8; '
                '[ -z "$f" ] || exit 9; '
                'rm -rf -- "$p"'
            )
            result = await self._run(
                policy,
                "tmp_cleanup.delete",
                host,
                command,
                timeout=timeout,
                reason=reason,
            )
            entry = {
                "path": path,
                "bytes": row["bytes"],
                "exit_code": result["exit_code"],
            }
            if result["exit_code"] == 0 and not result["timed_out"]:
                deleted.append(entry)
                outcome = "deleted"
                with _SCANS_LOCK:
                    scan.candidates.pop(path, None)
            else:
                kept.append(
                    {
                        **entry,
                        "reason": "delete_guard_or_rm_failed",
                        "stderr": result["stderr"],
                    }
                )
                outcome = "delete_failed"
            await self._ops._record(
                policy,
                {
                    "tool": "tmp_cleanup",
                    "outcome": outcome,
                    "host": host.name,
                    "reason": reason,
                    "token_sha256": _digest_token(token),
                    "detail": json.dumps(entry, default=str),
                },
            )
        return {
            "dry_run": False,
            "host": host.name,
            "deleted": deleted,
            "freed_bytes": sum(d["bytes"] or 0 for d in deleted),
            "kept": kept,
        }

    # ---- registration ----------------------------------------------------

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.probe",
                    description=(
                        "Run one read-only, fixed-template probe on an allowlisted lab "
                        "host: disk_free(host, path), mem_available(host), "
                        "tcp_reachable(host, target, port, timeout_seconds), "
                        "http_status(url, method HEAD|GET), tls_chain(target, port, "
                        "servername), vpn_configs(host, macOS), listening_ports(host, "
                        "port?), lsof_port(host, port), lease_state (operator-configured "
                        "command), host_health(host). kind=verify_blocker with claim_kind "
                        "(no_vpn_config, tls_intercepted, disk_full, host_unreachable, "
                        "endpoint_unreachable) runs the matching probe and returns "
                        "claim_supported true/false/null with the evidence. Network "
                        "targets must be lab hosts or configured external hosts. "
                        "Returns parsed JSON plus redacted raw output. Audited."
                    ),
                    input_schema=_PROBE_SCHEMA,
                    output_schema=_OUT,
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.SAFE,
                ),
                self.probe,
            ),
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.tmp_cleanup",
                    description=(
                        "Find and delete idle full-repository exports under the "
                        "operator's configured cleanup roots: direct child directories "
                        "with a .git directory or at least the size threshold, no file "
                        "modified within the idle limit, and no process holding a file "
                        "open (lsof). dry_run defaults to true and returns candidates, "
                        "sizes and a token. To delete, call with dry_run=false, that "
                        "token (valid 15 minutes) and a subset of its candidate paths; "
                        "idleness is checked again before each delete. Symlinks are "
                        "never followed; protected paths are never touched. Audited."
                    ),
                    input_schema=_CLEANUP_SCHEMA,
                    output_schema=_OUT,
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.tmp_cleanup,
            ),
        ]


def _digest_token(token: str) -> str:
    return digest(token)


def _reset_cleanup_tokens() -> None:
    """Test hook: forget every dry-run token."""

    with _SCANS_LOCK:
        _SCANS.clear()


_HOST_FIELD = {"type": "string", "minLength": 1, "maxLength": 64}
_OUT = {"type": "object", "additionalProperties": True}
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(PROBE_KINDS)},
        "host": _HOST_FIELD,
        "path": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "target": {"type": "string", "minLength": 1, "maxLength": 253},
        "port": {"type": "integer", "minimum": 1, "maximum": 65_535},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        "url": {"type": "string", "minLength": 8, "maxLength": 1_200},
        "method": {"type": "string", "enum": ["HEAD", "GET"]},
        "servername": {"type": "string", "minLength": 1, "maxLength": 253},
        "claim_kind": {"type": "string", "enum": sorted(BLOCKER_PROBES)},
        "name_substring": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_issuer_substring": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
        },
        "min_free_bytes": {"type": "integer", "minimum": 0},
        "reason": {"type": "string", "minLength": 3, "maxLength": 500},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
_CLEANUP_SCHEMA = {
    "type": "object",
    "properties": {
        "dry_run": {"type": "boolean"},
        "token": {"type": "string", "minLength": 8, "maxLength": 64},
        "paths": {
            "type": "array",
            "items": {"type": "string", "minLength": 2, "maxLength": 1_000},
            "minItems": 1,
            "maxItems": 200,
        },
        "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 1_800},
        "reason": {"type": "string", "minLength": 3, "maxLength": 500},
    },
    "required": ["reason"],
    "additionalProperties": False,
}
