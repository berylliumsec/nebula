"""Operator policy, guardrails and audit log for Butler's supervisor powers.

Butler's supervisor tools (host commands, process signals, runner control,
repository publish, gate runs, repository forensics, independent review and goal
hygiene) are off unless the operator sets ``NEBULA_BUTLER_SUPERVISOR_ENABLED``.
Clearing it is the kill switch: every tool re-reads the policy on each call and
refuses while it is off, so no restart is needed to stop Butler.

The guardrails here are enforced in code before any command runs:

- only allowlisted lab hosts are reachable, and network or radio tools may not
  name any address outside the allowlist;
- the production mirror is pull-only: commands that could write it, and git
  writes or pushes there, are refused;
- operator-only actions (submitting reports or uploads to outside services,
  account and credential changes, platform integrity settings, firmware and
  hardware power, plus any operator-configured patterns) are refused;
- configured private strings (such as the operator's email address) never leave
  the host in a command and are redacted from returned output;
- git worktrees are refused.

String screening of shell text is a guardrail, not a sandbox. The audit log
records every attempt, allowed or refused, so the operator can review it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ENABLED_ENV = "NEBULA_BUTLER_SUPERVISOR_ENABLED"
HOSTS_ENV = "NEBULA_BUTLER_SUPERVISOR_HOSTS"
ALLOW_SUDO_ENV = "NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO"
AUDIT_LOG_ENV = "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG"
PROTECTED_PATHS_ENV = "NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS"
PRIVATE_STRINGS_ENV = "NEBULA_BUTLER_SUPERVISOR_PRIVATE_STRINGS"
EXTERNAL_HOSTS_ENV = "NEBULA_BUTLER_SUPERVISOR_EXTERNAL_HOSTS"
RUNNER_CONTROL_ENV = "NEBULA_BUTLER_SUPERVISOR_RUNNER_CONTROL"
INGRESS_CONTROL_ENV = "NEBULA_BUTLER_SUPERVISOR_INGRESS_CONTROL"
PUBLISH_SCRIPT_ENV = "NEBULA_BUTLER_SUPERVISOR_PUBLISH_SCRIPT"
PREPARE_SCRIPT_ENV = "NEBULA_BUTLER_SUPERVISOR_PREPARE_SCRIPT"
OPERATOR_ONLY_ENV = "NEBULA_BUTLER_SUPERVISOR_OPERATOR_ONLY_PATTERNS"
GATE_ROOTS_ENV = "NEBULA_BUTLER_SUPERVISOR_GATE_ROOTS"
FORENSICS_REPOS_ENV = "NEBULA_BUTLER_SUPERVISOR_FORENSICS_REPOS"
REVIEW_PROVIDER_ENV = "NEBULA_BUTLER_REVIEW_PROVIDER_ID"
REVIEW_MODEL_ENV = "NEBULA_BUTLER_REVIEW_MODEL"
CEILING_POLICY_ENV = "NEBULA_BUTLER_REVIEW_CEILING_POLICY"
EVIDENCE_ROOTS_ENV = "NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS"
HYGIENE_PROJECTS_ENV = "NEBULA_BUTLER_HYGIENE_PROJECT_IDS"
HYGIENE_UNLIMITED_ENV = "NEBULA_BUTLER_HYGIENE_UNLIMITED_BUDGETS"
HYGIENE_STAGE_ENV = "NEBULA_BUTLER_HYGIENE_DEFAULT_STAGE"

DEFAULT_REVIEW_MODEL = "claude-opus-5-5"
DEFAULT_EXTERNAL_HOSTS = ("github.com",)
MAX_OUTPUT_BYTES = 64_000

_TRUE = {"1", "true", "yes", "on"}


class SupervisorDisabled(PermissionError):
    """The operator kill switch is off."""


class GuardrailViolation(PermissionError):
    """A command or target is refused by a code-enforced guardrail."""

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail}")


@dataclass(frozen=True)
class LabHost:
    """One allowlisted host. ``local`` runs on the Core host itself."""

    name: str
    address: str | None = None
    user: str | None = None
    identity_file: str | None = None
    local: bool = False
    port: int | None = None
    # Radio tools (Wi-Fi, Bluetooth, SDR) run only on hosts the operator marks
    # as inside the shielded lab.
    rf_lab: bool = False

    def addresses(self) -> set[str]:
        values = {self.name.casefold()}
        if self.address:
            values.add(self.address.casefold())
        return values


@dataclass(frozen=True)
class SupervisorPolicy:
    enabled: bool
    hosts: dict[str, LabHost]
    allow_sudo: bool
    audit_log: Path
    protected_paths: tuple[str, ...]
    private_strings: tuple[str, ...]
    external_hosts: tuple[str, ...]
    runner_control: str | None
    ingress_control: str | None
    publish_script: str | None
    prepare_script: str | None
    operator_only_patterns: tuple[str, ...]
    gate_roots: tuple[str, ...]
    forensics_repos: tuple[str, ...]
    review_provider_id: str | None
    review_model: str
    ceiling_policy_path: str | None
    evidence_roots: tuple[str, ...]
    hygiene_project_ids: tuple[str, ...]
    hygiene_unlimited_budgets: bool
    hygiene_default_stage: str
    extra: dict[str, Any] = field(default_factory=dict)

    def require_enabled(self) -> None:
        if not self.enabled:
            raise SupervisorDisabled(
                "Butler supervisor tools are off. The operator enables them with "
                f"{ENABLED_ENV}=1; clearing it is the kill switch."
            )

    def host(self, name: str) -> LabHost:
        host = self.hosts.get(name)
        if host is None:
            raise GuardrailViolation(
                "host_not_allowlisted",
                f"{name!r} is not a lab host; allowlisted: {sorted(self.hosts)}",
            )
        return host

    def lab_addresses(self) -> set[str]:
        values: set[str] = {"localhost", "127.0.0.1", "::1"}
        for host in self.hosts.values():
            values |= host.addresses()
        return values


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in re.split(r"[,:\n]", value) if item.strip())


def _split_commas(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_hosts(raw: str | None) -> dict[str, LabHost]:
    """Parse the JSON host allowlist.

    ``{"core": {"local": true}, "responder": {"address": "192.0.2.10",
    "user": "lab", "identity_file": "~/.ssh/id_lab"}}``
    """

    if not raw:
        return {"core": LabHost(name="core", local=True)}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{HOSTS_ENV} must be a JSON object")
    hosts: dict[str, LabHost] = {}
    for name, spec in data.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
            raise ValueError(f"invalid lab host name {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"lab host {name!r} must be an object")
        local = bool(spec.get("local", False))
        address = spec.get("address")
        if not local and not isinstance(address, str):
            raise ValueError(f"remote lab host {name!r} needs an address")
        hosts[name] = LabHost(
            name=name,
            address=address,
            user=spec.get("user"),
            identity_file=(
                str(Path(spec["identity_file"]).expanduser())
                if spec.get("identity_file")
                else None
            ),
            local=local,
            port=int(spec["port"]) if spec.get("port") else None,
            rf_lab=bool(spec.get("rf_lab", False)),
        )
    return hosts


def load_policy(environ: dict[str, str] | None = None) -> SupervisorPolicy:
    env = os.environ if environ is None else environ
    data_dir = Path(
        env.get("NEBULA_V3_DATA_DIR", str(Path.home() / ".local/share/nebula/v3"))
    )
    return SupervisorPolicy(
        enabled=env.get(ENABLED_ENV, "").strip().casefold() in _TRUE,
        hosts=_parse_hosts(env.get(HOSTS_ENV)),
        allow_sudo=env.get(ALLOW_SUDO_ENV, "").strip().casefold() in _TRUE,
        audit_log=Path(
            env.get(AUDIT_LOG_ENV) or data_dir / "butler-supervisor-audit.jsonl"
        ),
        protected_paths=_split_commas(env.get(PROTECTED_PATHS_ENV)),
        private_strings=_split_commas(env.get(PRIVATE_STRINGS_ENV)),
        external_hosts=_split_commas(env.get(EXTERNAL_HOSTS_ENV))
        or DEFAULT_EXTERNAL_HOSTS,
        runner_control=env.get(RUNNER_CONTROL_ENV) or None,
        ingress_control=env.get(INGRESS_CONTROL_ENV) or None,
        publish_script=env.get(PUBLISH_SCRIPT_ENV) or None,
        prepare_script=env.get(PREPARE_SCRIPT_ENV) or None,
        operator_only_patterns=tuple(
            item
            for item in (env.get(OPERATOR_ONLY_ENV) or "").splitlines()
            if item.strip()
        ),
        gate_roots=_split_commas(env.get(GATE_ROOTS_ENV)),
        forensics_repos=_split_commas(env.get(FORENSICS_REPOS_ENV)),
        review_provider_id=env.get(REVIEW_PROVIDER_ENV) or None,
        review_model=env.get(REVIEW_MODEL_ENV) or DEFAULT_REVIEW_MODEL,
        ceiling_policy_path=env.get(CEILING_POLICY_ENV) or None,
        evidence_roots=_split_commas(env.get(EVIDENCE_ROOTS_ENV)),
        hygiene_project_ids=_split_commas(env.get(HYGIENE_PROJECTS_ENV)),
        hygiene_unlimited_budgets=env.get(HYGIENE_UNLIMITED_ENV, "").strip().casefold()
        in _TRUE,
        hygiene_default_stage=env.get(HYGIENE_STAGE_ENV) or "triage",
    )


# --------------------------------------------------------------------------
# Command guardrails
# --------------------------------------------------------------------------

# Operator-only actions. Each entry: (rule, pattern). Patterns run on the
# case-folded command text.
_OPERATOR_ONLY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("operator_only_sip", re.compile(r"\bcsrutil\b")),
    (
        "operator_only_firmware",
        re.compile(
            r"\b(nvram|bless|kmutil\s+(trigger-panic-medic|configure-boot)|"
            r"flashrom|fwupdmgr|idevicerestore|cfgutil\s+(restore|revive|erase)|"
            r"asr\s+restore|dfu-util)\b"
        ),
    ),
    (
        "operator_only_hardware_power",
        re.compile(
            r"\b(shutdown|poweroff|halt|reboot|uhubctl|pmset\s+(sleepnow|restoredefaults)|"
            r"systemctl\s+(poweroff|reboot|halt|kexec))\b"
        ),
    ),
    (
        "operator_only_account",
        re.compile(
            r"(\bdscl\b.*\b(passwd|-create|-delete)\b|\bsysadminctl\b|"
            r"\bpasswd\b|\bchpasswd\b|\busermod\b|\bprofiles\s+(install|remove)|"
            r"\bsecurity\s+(delete|add)-(internet|generic)-password)"
        ),
    ),
    (
        "operator_only_report_submission",
        re.compile(r"(\b(sendmail|mutt|mailx|swaks)\b|\bmail\s+-s\b)"),
    ),
)

# Uploads to a host outside the lab (report portals, issue trackers, mail
# relays) are the operator's to send. git push to an allowlisted external host
# is the only outbound write.
_UPLOAD = re.compile(
    r"(\bcurl\b[^|;&]*\s(-d|--data\S*|-F|--form\S*|-T|--upload-file|-X\s*(POST|PUT|PATCH))\b|"
    r"\bwget\b[^|;&]*--(post|body)-(data|file)|\bscp\b|\bsftp\b|\brsync\b|"
    r"\bgh\s+(issue|pr|release|gist|api)\b)"
)

_WORKTREE = re.compile(r"\bgit\b[^|;&]*\bworktree\s+(add|move)\b")

# Decoding text into a shell defeats screening; refuse it outright.
_OBFUSCATED = re.compile(
    r"(base64\s+(-d|--decode)[^|]*\|\s*(ba|z|da)?sh\b|\bxxd\s+-r[^|]*\|\s*(ba)?sh\b|"
    r"\beval\s+[\"'$`(]|\|\s*(ba|z|da)?sh\s*$|\bpython3?\s+-c\s+[\"'].*\b(exec|eval)\()"
)

_SUDO = re.compile(r"(^|[\s;&|(`])sudo\b")

# Radio or attack tooling: allowed only on hosts marked ``rf_lab``.
_RF_TOOLS = re.compile(
    r"\b(aireplay-ng|aircrack-ng|airodump-ng|mdk[34]|wifite|bettercap|hackrf_\w+|"
    r"ubertooth-\w+|hcitool|btmgmt|l2ping|gatttool|hostapd|wpa_supplicant|"
    r"iw\s+dev\s+\S+\s+(set|scan)|rfkill\s+unblock|gr-\w+|uhd_\w+|bladerf-cli)\b"
)

# Tools that make network connections. Every address they name must be a lab
# host or an allowlisted external host (for example GitHub for a publish).
_NETWORK_TOOLS = re.compile(
    r"\b(curl|wget|nc|ncat|netcat|socat|ssh|scp|sftp|rsync|nmap|masscan|"
    r"hping3|telnet|ftp|http|httpie|ping|traceroute|dig|nslookup|openssl\s+s_client|"
    r"git\s+(push|fetch|clone|pull|ls-remote))\b"
)

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_HOSTNAME = re.compile(
    r"(?:(?<=@)|(?<=://)|(?<=\s))([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)(?=[:/\s'\"]|$)"
)
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}")

_FILE_SUFFIXES = (
    ".py",
    ".sh",
    ".json",
    ".md",
    ".txt",
    ".log",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".html",
    ".c",
    ".m",
    ".h",
    ".ips",
    ".bin",
    ".tar",
    ".gz",
    ".zip",
    ".plist",
    ".pdf",
    ".png",
    ".jpg",
    ".csv",
    ".xml",
    ".patch",
    ".diff",
)

# Read-only commands permitted against a protected (pull-only) path.
_READ_ONLY_GIT = re.compile(
    r"^git\s+(-C\s+\S+\s+)?(status|log|show|diff|rev-parse|ls-files|ls-tree|"
    r"cat-file|branch\s+(--show-current|-a|-r|--list)|remote\s+-v|fetch|describe|"
    r"blame|grep)\b"
)
_READ_ONLY_TOOLS = {
    "cat",
    "ls",
    "head",
    "tail",
    "stat",
    "sha256sum",
    "wc",
    "file",
    "readlink",
    "realpath",
    "test",
    "[",
    "du",
    "diff",
    "cmp",
    "grep",
    "rg",
    "jq",
    "find",
}


def _segments(command: str) -> list[str]:
    """Split a shell command into simple-command segments for screening."""

    parts = re.split(r"\|\||&&|[;|\n]", command)
    return [part.strip() for part in parts if part.strip()]


def _segment_is_read_only(segment: str) -> bool:
    if _READ_ONLY_GIT.match(segment):
        return True
    if re.search(r"(^|[^<>&0-9])>{1,2}", segment) or "tee " in segment:
        return False
    try:
        words = shlex.split(segment)
    except ValueError:
        return False
    if not words:
        return True
    tool = os.path.basename(words[0])
    if tool == "sed":
        return "-i" not in words and not any(w.startswith("-i") for w in words[1:])
    if tool == "find":
        return not any(w in {"-delete", "-exec", "-execdir", "-ok"} for w in words)
    return tool in _READ_ONLY_TOOLS


def _refers_to(path: str, text: str) -> bool:
    root = path.rstrip("/")
    return re.search(re.escape(root) + r"(?=$|[/\s'\";|&)])", text) is not None


def _addresses_named(segment: str) -> set[str]:
    found = {match.group(1) for match in _IPV4.finditer(segment)}
    for match in _HOSTNAME.finditer(" " + segment + " "):
        name = match.group(1).strip(".")
        if name.endswith(_FILE_SUFFIXES):
            continue
        # Skip dotted Python/module-like words with no letters after a dot.
        if re.fullmatch(r"[0-9.]+", name):
            continue
        found.add(name)
    return found


def _is_private_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_loopback


def screen_command(
    policy: SupervisorPolicy,
    command: str,
    *,
    host: LabHost,
    cwd: str | None = None,
) -> None:
    """Refuse a command that breaks a guardrail. Raises GuardrailViolation."""

    if not command.strip():
        raise GuardrailViolation("empty_command", "command is empty")
    if "\x00" in command:
        raise GuardrailViolation("invalid_command", "command contains a NUL byte")
    text = command.casefold()

    for secret in policy.private_strings:
        if secret and secret.casefold() in text:
            raise GuardrailViolation(
                "private_data",
                "command contains a configured private string; it may not leave the host",
            )

    for rule, pattern in _OPERATOR_ONLY:
        if pattern.search(text):
            raise GuardrailViolation(
                rule, "operator-only action; the human operator must do this"
            )
    for raw in policy.operator_only_patterns:
        try:
            matched = re.search(raw, text, re.IGNORECASE) is not None
        except re.error:
            # A malformed operator pattern fails closed.
            matched = True
        if matched:
            raise GuardrailViolation(
                "operator_only_configured",
                "matches an operator-only pattern; the human operator must do this",
            )
    if re.search(r"\bgh\s+(issue|pr|release|gist|api)\b", text):
        raise GuardrailViolation(
            "operator_only_report_submission",
            "posting to an outside service is the operator's to do",
        )

    if _WORKTREE.search(text):
        raise GuardrailViolation("git_worktree", "git worktrees are not allowed")

    if _OBFUSCATED.search(text):
        raise GuardrailViolation(
            "obfuscated_command",
            "decoding text into a shell, eval, or piping into a shell is refused; "
            "write the command out plainly",
        )

    if _SUDO.search(text) and not policy.allow_sudo:
        raise GuardrailViolation(
            "sudo_disabled",
            "general sudo is off; use the runner control tool, or the operator sets "
            f"{ALLOW_SUDO_ENV}=1",
        )

    if _RF_TOOLS.search(text) and not host.rf_lab:
        raise GuardrailViolation(
            "rf_outside_lab",
            f"radio tools may run only on a host marked rf_lab; {host.name} is not",
        )

    lab = policy.lab_addresses()
    external = {item.casefold() for item in policy.external_hosts}
    # Flags are case-sensitive (curl -F uploads, -f does not); hosts are not.
    for original in _segments(command):
        segment = original.casefold()
        if not _NETWORK_TOOLS.search(segment):
            continue
        for address in _addresses_named(segment):
            name = address.casefold()
            if name in lab or _is_private_ip(name):
                continue
            if name in external or any(name.endswith("." + item) for item in external):
                if _UPLOAD.search(original):
                    raise GuardrailViolation(
                        "operator_only_report_submission",
                        f"uploading to {address!r} outside the lab is the operator's to do",
                    )
                if re.search(r"\bgit\s+push\b|\bcurl\b|\bwget\b", segment) and (
                    _EMAIL.search(segment)
                ):
                    raise GuardrailViolation(
                        "private_data", "an email address may not be sent externally"
                    )
                continue
            raise GuardrailViolation(
                "host_not_allowlisted",
                f"network tool names {address!r}, which is not a lab host",
            )

    for protected in policy.protected_paths:
        root = protected.rstrip("/")
        inside_cwd = cwd is not None and (
            cwd.rstrip("/") == root or cwd.startswith(root + "/")
        )
        if not inside_cwd and not _refers_to(root, command):
            continue
        for segment in _segments(command):
            if not inside_cwd and not _refers_to(root, segment):
                continue
            if not _segment_is_read_only(segment):
                raise GuardrailViolation(
                    "production_mirror_read_only",
                    f"{root} is the pull-only production mirror; only read-only "
                    "commands may touch it, and git writes or pushes are refused",
                )


def screen_path_write(policy: SupervisorPolicy, path: str | Path) -> None:
    resolved = os.path.realpath(str(path))
    for protected in policy.protected_paths:
        root = os.path.realpath(protected)
        if resolved == root or resolved.startswith(root.rstrip("/") + "/"):
            raise GuardrailViolation(
                "production_mirror_read_only",
                f"{protected} is the pull-only production mirror",
            )


def redact(policy: SupervisorPolicy, text: str) -> str:
    for secret in policy.private_strings:
        if secret:
            text = re.sub(re.escape(secret), "[redacted]", text, flags=re.IGNORECASE)
    return text


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()


def digest(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def write_audit(policy: SupervisorPolicy, record: dict[str, Any]) -> dict[str, Any]:
    """Append one durable JSON line. The command text is redacted first."""

    entry = {
        "schema": "nebula.butler-supervisor-audit/v1",
        "at": datetime.now(UTC).isoformat(),
        **record,
    }
    for key in ("command", "detail"):
        if isinstance(entry.get(key), str):
            entry[key] = redact(policy, entry[key])
    line = json.dumps(entry, sort_keys=True, default=str) + "\n"
    policy.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOCK:
        fd = os.open(policy.audit_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return entry
