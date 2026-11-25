"""Check Work check-in text for commit hashes and sign-offs that do not exist.

Agents sometimes cite commit hashes that were never created, or claim a review
sign-off that no reviewer gave.  This guard resolves every cited hash against
the project's configured Git repositories and asks the review-gating module
for forged sign-off phrases.  It reports what it found; the caller decides
whether to flag or reject the check-in.

Configuration (environment):

``NEBULA_FABRICATION_GUARD_MODE``
    ``off``, ``flag`` (default) or ``reject``.
``NEBULA_FABRICATION_GUARD_REPOS``
    JSON object ``{"<project id>": ["<repo path>", ...]}``.  The key ``"*"``
    applies to every project without its own entry.

A hash is real when it resolves to a commit in ANY configured repository.  A
hash is only called a phantom when every configured repository answered and
none had it; a Git failure leaves the hash ``unverified`` instead.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Literal

from .diagnostics import record_caught_exception

GuardMode = Literal["off", "flag", "reject"]
SignoffCheck = Literal["checked", "unavailable", "failed", "off"]

MODE_ENV = "NEBULA_FABRICATION_GUARD_MODE"
REPOS_ENV = "NEBULA_FABRICATION_GUARD_REPOS"
DEFAULT_MODE: GuardMode = "flag"
WILDCARD_PROJECT = "*"

# Full (40) or abbreviated (12) lowercase SHA-1 commit names.  The lookarounds
# reject UUID groups (joined by ``-``) and runs inside longer hex or words, so a
# 64-character digest or ``…-426614174000`` never yields a 12-character match.
COMMIT_HASH = re.compile(
    r"(?<![0-9A-Za-z_-])([0-9a-f]{12}|[0-9a-f]{40})(?![0-9A-Za-z_-])"
)
# A short hex run directly after a digest label is an artifact digest prefix,
# not a commit.
_DIGEST_LABEL = re.compile(r"(?:sha-?256|sha-?512|sha-?1|md5|digest)[\s:=]*$", re.I)
MAX_HASHES_CHECKED = 32
GIT_TIMEOUT_SECONDS = 5.0


class FabricationRejected(ValueError):
    """A check-in cited commits or sign-offs that the guard could not find."""

    def __init__(
        self, phantom_hashes: Sequence[str], forged_signoffs: Sequence[str]
    ) -> None:
        self.phantom_hashes = list(phantom_hashes)
        self.forged_signoffs = list(forged_signoffs)
        parts: list[str] = []
        if self.phantom_hashes:
            parts.append(
                "commit hash(es) not found in any configured repository: "
                + ", ".join(self.phantom_hashes)
            )
        if self.forged_signoffs:
            parts.append(
                "unsupported review sign-off(s): " + "; ".join(self.forged_signoffs)
            )
        super().__init__(
            "check-in rejected by the fabrication guard: "
            + " | ".join(parts)
            + ". Cite only commits that exist and sign-offs a reviewer gave."
        )


@dataclass
class GuardResult:
    """What the guard checked and found for one check-in."""

    mode: GuardMode
    checked: bool = False
    skipped_reason: str | None = None
    phantom_hashes: list[str] = field(default_factory=list)
    unverified_hashes: list[str] = field(default_factory=list)
    unchecked_hash_count: int = 0
    forged_signoffs: list[str] = field(default_factory=list)
    signoff_check: SignoffCheck = "off"
    errors: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.phantom_hashes or self.forged_signoffs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "checked": self.checked,
            "skipped_reason": self.skipped_reason,
            "flagged": self.flagged,
            "phantom_hashes": list(self.phantom_hashes),
            "unverified_hashes": list(self.unverified_hashes),
            "unchecked_hash_count": self.unchecked_hash_count,
            "forged_signoffs": list(self.forged_signoffs),
            "signoff_check": self.signoff_check,
            "errors": list(self.errors),
        }

    def note(self) -> str:
        """A short, human-readable line that can be attached to the check-in."""

        parts: list[str] = []
        if self.phantom_hashes:
            parts.append(
                "commit hash(es) not found in configured repositories: "
                + ", ".join(self.phantom_hashes)
            )
        if self.forged_signoffs:
            parts.append("unsupported sign-off(s): " + "; ".join(self.forged_signoffs))
        return "[Nebula fabrication guard: " + " | ".join(parts) + "]"


def guard_mode(environ: Mapping[str, str] | None = None) -> GuardMode:
    raw = (environ if environ is not None else os.environ).get(MODE_ENV, "")
    value = raw.strip().lower()
    if value == "off":
        return "off"
    if value == "reject":
        return "reject"
    return DEFAULT_MODE


def configured_repos(
    project_id: str, environ: Mapping[str, str] | None = None
) -> list[Path]:
    """Repositories configured for a project; the ``*`` entry is the fallback."""

    raw = (environ if environ is not None else os.environ).get(REPOS_ENV, "")
    if not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        record_caught_exception(
            "projects",
            "projects.fabrication_guard_config_invalid",
            "The fabrication guard repository setting is not valid JSON.",
            exc,
            stage="configuration",
        )
        return []
    if not isinstance(decoded, dict):
        return []
    entry = decoded.get(project_id, decoded.get(WILDCARD_PROJECT))
    if isinstance(entry, str):
        entry = [entry]
    if not isinstance(entry, list):
        return []
    return [Path(item).expanduser() for item in entry if isinstance(item, str) and item]


def extract_commit_hashes(text: str) -> list[str]:
    """Candidate commit hashes in first-seen order, without digest prefixes."""

    seen: dict[str, None] = {}
    for match in COMMIT_HASH.finditer(text):
        value = match.group(1)
        # All-digit runs are numbers and all-letter runs are words.
        if not re.search(r"[a-f]", value) or not re.search(r"[0-9]", value):
            continue
        if _DIGEST_LABEL.search(text[max(0, match.start() - 12) : match.start()]):
            continue
        seen.setdefault(value, None)
    return list(seen)


GitRunner = Callable[[Path, Sequence[str], float], dict[str, bool]]


def _git_resolve(repo: Path, hashes: Sequence[str], timeout: float) -> dict[str, bool]:
    """Map each hash to whether it names a commit in ``repo``.

    One ``cat-file --batch-check`` call per repository keeps the cost bounded.
    An ambiguous abbreviation means at least one commit matched, so it counts
    as present.  Raises ``OSError``/``subprocess`` errors to the caller.
    """

    request = "".join(f"{value}^{{commit}}\n" for value in hashes)
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch-check"],
        input=request,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != len(hashes):
        raise subprocess.SubprocessError("git cat-file returned a partial answer")
    found: dict[str, bool] = {}
    for value, line in zip(hashes, lines, strict=True):
        found[value] = not line.endswith(" missing")
    return found


def find_phantom_hashes(
    text: str,
    repos: Sequence[Path | str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_hashes: int = MAX_HASHES_CHECKED,
    runner: GitRunner | None = None,
    result: GuardResult | None = None,
) -> list[str]:
    """Hashes in ``text`` that no configured repository contains.

    Hashes that could not be checked because a repository failed are recorded
    on ``result`` (when given) as unverified, never as phantoms.
    """

    resolve = runner or _git_resolve
    candidates = extract_commit_hashes(text)
    checked = candidates[:max_hashes]
    if result is not None:
        result.unchecked_hash_count = len(candidates) - len(checked)
    if not checked or not repos:
        return []
    present: set[str] = set()
    failed_repos = 0
    for repo in repos:
        pending = [value for value in checked if value not in present]
        if not pending:
            break
        try:
            answers = resolve(Path(repo), pending, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            # A missing git, a timeout, or a non-repository leaves these hashes
            # unverified; the guard must never fail the check-in for it.
            failed_repos += 1
            record_caught_exception(
                "projects",
                "projects.fabrication_guard_git_failed",
                "The fabrication guard could not query a configured repository.",
                exc,
                stage="git-resolve",
            )
            if result is not None:
                result.errors.append(f"{type(exc).__name__} for a configured repo")
            continue
        present.update(value for value, exists in answers.items() if exists)
    missing = [value for value in checked if value not in present]
    if failed_repos:
        if result is not None:
            result.unverified_hashes.extend(missing)
        return []
    return missing


REVIEW_GATING_MODULE = "butler_review_gating"


def detect_signoffs(text: str) -> tuple[list[str], SignoffCheck]:
    """Forged sign-offs from the review-gating module, if it is installed."""

    target = f"{__package__}.{REVIEW_GATING_MODULE}"
    try:
        module = importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name != target:
            raise
        # diagnostic-expected: review gating ships separately; without it the
        # guard reports the sign-off check as unavailable.
        return [], "unavailable"
    detect = getattr(module, "detect_forged_signoff", None)
    if not callable(detect):
        return [], "unavailable"
    try:
        found = detect(text)
    except Exception as exc:
        record_caught_exception(
            "projects",
            "projects.fabrication_guard_signoff_failed",
            "The forged sign-off check failed; the check-in was not blocked.",
            exc,
            stage="signoff",
        )
        return [], "failed"
    return [str(item) for item in found or ()], "checked"


def check_text(
    project_id: str,
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    runner: GitRunner | None = None,
) -> GuardResult:
    """Run every configured check on one check-in's text.  Never raises."""

    mode = guard_mode(environ)
    result = GuardResult(mode=mode)
    if mode == "off":
        result.skipped_reason = "guard mode is off"
        return result
    result.forged_signoffs, result.signoff_check = detect_signoffs(text)
    repos = configured_repos(project_id, environ)
    if not repos:
        result.skipped_reason = (
            f"no repository configured for this project in {REPOS_ENV}; "
            "commit hashes were not checked"
        )
        result.checked = result.signoff_check == "checked"
        return result
    result.phantom_hashes = find_phantom_hashes(
        text, repos, runner=runner, result=result
    )
    result.checked = True
    return result


__all__ = [
    "COMMIT_HASH",
    "FabricationRejected",
    "GuardMode",
    "GuardResult",
    "MODE_ENV",
    "REPOS_ENV",
    "check_text",
    "configured_repos",
    "detect_signoffs",
    "extract_commit_hashes",
    "find_phantom_hashes",
    "guard_mode",
]
