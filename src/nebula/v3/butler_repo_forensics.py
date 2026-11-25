"""Repository forensics and exact-path restore for Butler's supervisor powers.

Many autonomous research goals commit into one shared git checkout. Each goal
owns ``<project_root>/<project_name>/...``. A goal that stages too widely can
sweep another goal's files into its commit, delete them, or write an older
blob back over a newer one (a stale revert). ``repo_audit`` finds these in a
commit range; ``repo_restore`` puts named paths back to the content of one
known-good commit and commits only those paths.

Every git call uses an argument list (no shell), ``core.quotepath=off``,
literal pathspecs and a timeout. The restore never pushes, never bypasses
hooks, and never commits a path it was not given.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .butler_supervisor_policy import (
    MAX_OUTPUT_BYTES,
    GuardrailViolation,
    SupervisorDisabled,
    SupervisorPolicy,
    load_policy,
    redact,
    screen_path_write,
    write_audit,
)
from .diagnostics import record_caught_exception
from .domain import RiskClass
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


AUDIT_TOOL = "nebula.supervisor.repo_audit"
RESTORE_TOOL = "nebula.supervisor.repo_restore"

DEFAULT_RANGE = "HEAD~50..HEAD"
DEFAULT_PROJECT_ROOT = "projects"
MAX_COMMITS = 500
MAX_PATHS_PER_COMMIT = 400
MAX_HISTORY_LOOKUPS = 3000
MAX_HISTORY_DEPTH = 2000
MAX_FINDINGS = 2000
MAX_RESTORE_PATHS = 50
GIT_TIMEOUT_SECONDS = 60
TRAILER_KEY = "Nebula-Project"

_REV_SIDE = r"[A-Za-z0-9_][A-Za-z0-9_./~^@{}-]{0,199}"
_RANGE = re.compile(rf"^{_REV_SIDE}(?:\.\.\.?{_REV_SIDE})?$")
_HEX_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_GLOB_CHARS = set("*?[]")
_SEP = "\x1f"
_REC = "\x1e"

_STRING_LIST = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 1024},
    "maxItems": 100,
}

_AUDIT_INPUT = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "minLength": 1, "maxLength": 4096},
        "rev_range": {"type": "string", "minLength": 1, "maxLength": 410},
        "project_root": {"type": "string", "minLength": 1, "maxLength": 200},
        "shared_paths": _STRING_LIST,
    },
    "required": ["repo"],
    "additionalProperties": False,
}

_RESTORE_INPUT = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "minLength": 1, "maxLength": 4096},
        "paths": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 4096},
            "minItems": 1,
            "maxItems": MAX_RESTORE_PATHS,
        },
        "source_commit": {"type": "string", "minLength": 7, "maxLength": 40},
        "message": {"type": "string", "minLength": 1, "maxLength": 10_000},
        "commit": {"type": "boolean", "default": True},
    },
    "required": ["repo", "paths", "source_commit", "message"],
    "additionalProperties": False,
}


class GitError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, output: str) -> None:
        self.git_args = args
        self.returncode = returncode
        self.output = output
        super().__init__(f"git {' '.join(args[:3])} failed ({returncode}): {output}")


@dataclass(frozen=True)
class _Change:
    status: str
    old_blob: str
    new_blob: str
    path: str
    old_path: str | None = None


def _git(
    repo: str,
    args: list[str],
    *,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SECONDS,
    read_only: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "GIT_PAGER": "cat",
    }
    if read_only:
        env["GIT_OPTIONAL_LOCKS"] = "0"
    command = [
        "git",
        "-c",
        "core.quotepath=off",
        "--literal-pathspecs",
        "-C",
        repo,
        *args,
    ]
    completed = subprocess.run(  # noqa: S603 - argument list, no shell
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitError(
            args,
            completed.returncode,
            (completed.stderr or completed.stdout)[-4000:].strip(),
        )
    return completed


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[: MAX_OUTPUT_BYTES // 2] + "\n...[truncated]...\n" + text[-2000:]


# --------------------------------------------------------------------------
# Argument validation
# --------------------------------------------------------------------------


def _resolve_repo(policy: SupervisorPolicy, repo: Any) -> str:
    if not isinstance(repo, str) or not repo or "\x00" in repo:
        raise GuardrailViolation("invalid_repo", "repo must be a path string")
    resolved = os.path.realpath(repo)
    allowed = {os.path.realpath(item) for item in policy.forensics_repos}
    if resolved not in allowed:
        raise GuardrailViolation(
            "repo_not_allowlisted",
            f"{repo!r} is not a forensics repo; the operator lists them in "
            "NEBULA_BUTLER_SUPERVISOR_FORENSICS_REPOS",
        )
    top = _git(resolved, ["rev-parse", "--show-toplevel"], check=False)
    if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != resolved:
        raise GuardrailViolation(
            "invalid_repo", f"{repo!r} is not the top level of a git checkout"
        )
    return resolved


def _validate_range(value: Any) -> str:
    if not isinstance(value, str) or not _RANGE.fullmatch(value):
        raise GuardrailViolation(
            "invalid_rev_range",
            "rev_range must look like 'A..B', 'A...B' or a single revision, "
            "with no leading '-'",
        )
    if "@{-" in value:
        raise GuardrailViolation("invalid_rev_range", f"unsupported range {value!r}")
    return value


def _validate_project_root(value: Any) -> str:
    if value is None:
        return DEFAULT_PROJECT_ROOT
    if not isinstance(value, str):
        raise GuardrailViolation("invalid_project_root", "project_root must be text")
    root = value.strip("/")
    parts = PurePosixPath(root).parts if root else ()
    if (
        not parts
        or value.startswith("/")
        or any(part in {".", ".."} for part in parts)
        or any(not _PROJECT_NAME.fullmatch(part) for part in parts)
    ):
        raise GuardrailViolation(
            "invalid_project_root", "project_root must be a plain relative directory"
        )
    return "/".join(parts)


def _validate_shared(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and "\x00" not in item for item in value
    ):
        raise GuardrailViolation(
            "invalid_shared_paths", "shared_paths must be a list of glob patterns"
        )
    return tuple(value)


def _validate_restore_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise GuardrailViolation("invalid_path", "each path must be non-empty text")
    reason = None
    if "\x00" in value or "\\" in value:
        reason = "contains a NUL or backslash"
    elif value.startswith("/") or os.path.isabs(value):
        reason = "is absolute"
    elif value.startswith("-"):
        reason = "starts with '-'"
    elif value.startswith(":"):
        reason = "starts with pathspec magic ':'"
    elif _GLOB_CHARS & set(value):
        reason = "contains a glob character"
    elif value.endswith("/"):
        reason = "names a directory"
    else:
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            reason = "contains '..', '.', or an empty component"
        elif parts[0] == ".git":
            reason = "is inside .git"
    if reason:
        raise GuardrailViolation(
            "invalid_path", f"{value!r} {reason}; give exact repo-relative file paths"
        )
    return value


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _project_of(path: str, project_root: str) -> str | None:
    prefix = project_root + "/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    name = rest.split("/", 1)[0]
    return name or None


def _is_shared(path: str, shared: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in shared)


def _parse_raw_z(output: str) -> list[_Change]:
    """Parse ``git diff-tree -r --raw -z`` output."""

    changes: list[_Change] = []
    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        header = tokens[index]
        index += 1
        if not header.startswith(":"):
            continue
        fields = header[1:].split()
        if len(fields) < 5:
            continue
        _old_mode, _new_mode, old_blob, new_blob, status = fields[:5]
        letter = status[:1]
        if letter in {"R", "C"}:
            if index + 1 >= len(tokens):
                break
            old_path, path = tokens[index], tokens[index + 1]
            index += 2
            changes.append(_Change(letter, old_blob, new_blob, path, old_path))
        else:
            if index >= len(tokens):
                break
            path = tokens[index]
            index += 1
            changes.append(_Change(letter, old_blob, new_blob, path))
    return changes


def _path_history(repo: str, start: str, path: str) -> list[tuple[str, str]]:
    """Newest-first ``(commit, blob)`` entries for ``path`` reachable from start."""

    result = _git(
        repo,
        [
            "log",
            f"-n{MAX_HISTORY_DEPTH}",
            "--format=commit %H",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            start,
            "--",
            path,
        ],
    )
    entries: list[tuple[str, str]] = []
    commit: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("commit "):
            commit = line[7:].strip()
        elif line.startswith(":") and commit is not None:
            fields = line[1:].split("\t", 1)[0].split()
            if len(fields) >= 5:
                entries.append((commit, fields[3]))
                commit = None
    return entries


def _deletions(repo: str, parent: str, commit: str, path: str) -> int | None:
    result = _git(
        repo,
        ["diff", "--numstat", "--no-renames", parent, commit, "--", path],
        check=False,
    )
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1].isdigit():
            return int(parts[1])
    return None


def _list_commits(repo: str, rev_range: str) -> tuple[list[dict[str, Any]], bool]:
    fmt = (
        _SEP.join(
            [
                "%H",
                "%P",
                "%an",
                "%s",
                f"%(trailers:key={TRAILER_KEY},valueonly,separator=%x2C)",
            ]
        )
        + _REC
    )
    result = _git(
        repo,
        [
            "log",
            "--no-merges",
            f"-n{MAX_COMMITS + 1}",
            f"--format={fmt}",
            rev_range,
            "--",
        ],
    )
    commits = []
    for record in result.stdout.split(_REC):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_SEP)
        if len(fields) < 5:
            continue
        trailer_values = [v.strip() for v in fields[4].split(",") if v.strip()]
        commits.append(
            {
                "commit": fields[0].strip(),
                "parents": fields[1].split(),
                "author": fields[2],
                "subject": fields[3],
                "trailer_project": trailer_values[-1] if trailer_values else None,
            }
        )
    truncated = len(commits) > MAX_COMMITS
    return commits[:MAX_COMMITS], truncated


def _range_resolves(repo: str, rev_range: str) -> bool:
    sides = re.split(r"\.\.\.?", rev_range)
    for side in sides:
        check = _git(
            repo,
            ["rev-parse", "--verify", "--quiet", f"{side}^{{commit}}"],
            check=False,
        )
        if check.returncode != 0:
            return False
    return True


def _audit_repo(
    repo: str,
    rev_range: str,
    project_root: str,
    shared: tuple[str, ...],
    default_range: bool,
) -> dict[str, Any]:
    effective_range = rev_range
    if not _range_resolves(repo, rev_range):
        if default_range and _range_resolves(repo, "HEAD"):
            effective_range = "HEAD"
        else:
            raise GuardrailViolation(
                "invalid_rev_range", f"{rev_range!r} does not resolve in this repo"
            )
    commits, commits_truncated = _list_commits(repo, effective_range)
    findings: list[dict[str, Any]] = []
    history_lookups = 0
    history_truncated = False
    paths_truncated: list[str] = []
    findings_truncated = False

    def add(finding: dict[str, Any]) -> None:
        nonlocal findings_truncated
        if len(findings) >= MAX_FINDINGS:
            findings_truncated = True
            return
        findings.append(finding)

    for info in commits:
        sha = info["commit"]
        parent = info["parents"][0] if info["parents"] else None
        raw = _git(
            repo,
            ["diff-tree", "-r", "--no-commit-id", "-M", "--raw", "--no-abbrev", "-z"]
            + ([] if parent else ["--root"])
            + [sha],
        )
        changes = _parse_raw_z(raw.stdout)
        if len(changes) > MAX_PATHS_PER_COMMIT:
            paths_truncated.append(sha)
            changes = changes[:MAX_PATHS_PER_COMMIT]

        def projects_for(change: _Change) -> set[str]:
            names = set()
            for candidate in (change.path, change.old_path):
                if candidate is None or _is_shared(candidate, shared):
                    continue
                name = _project_of(candidate, project_root)
                if name:
                    names.add(name)
            return names

        counts: Counter[str] = Counter()
        for change in changes:
            for name in projects_for(change):
                counts[name] += 1
        trailer = info["trailer_project"]
        if trailer:
            owner, owner_source = trailer, "trailer"
        elif counts:
            top = max(counts.values())
            owner = sorted(name for name, count in counts.items() if count == top)[0]
            owner_source = "most_changed_paths"
        else:
            owner, owner_source = None, "none"

        base = {
            "commit": sha,
            "subject": info["subject"],
            "author": info["author"],
            "owner_project": owner,
            "owner_source": owner_source,
        }

        foreign: list[dict[str, Any]] = []
        if owner is not None:
            for change in changes:
                others = sorted(projects_for(change) - {owner})
                if others:
                    foreign.append(
                        {
                            "path": change.path,
                            "old_path": change.old_path,
                            "status": change.status,
                            "project": others[0],
                        }
                    )
        if foreign:
            add(
                {
                    **base,
                    "kind": "wide_commit",
                    "projects_touched": sorted(counts),
                    "foreign_paths": [item["path"] for item in foreign],
                    "foreign_changes": foreign,
                }
            )
            for item in foreign:
                if item["status"] == "D":
                    add(
                        {
                            **base,
                            "kind": "foreign_delete",
                            "path": item["path"],
                            "project": item["project"],
                        }
                    )

        if parent is None:
            continue
        for change in changes:
            if change.status not in {"M", "T"} or change.new_blob == change.old_blob:
                continue
            if history_lookups >= MAX_HISTORY_LOOKUPS:
                history_truncated = True
                break
            history_lookups += 1
            history = _path_history(repo, parent, change.path)
            if not history:
                continue
            # The newest entry reachable from the parent is the commit that
            # last wrote the path: the good version this commit superseded.
            good_commit = history[0][0]
            stale_from = next(
                (commit for commit, blob in history[1:] if blob == change.new_blob),
                None,
            )
            if stale_from is None:
                continue
            project = _project_of(change.path, project_root)
            add(
                {
                    **base,
                    "kind": "stale_revert",
                    "path": change.path,
                    "project": project,
                    "foreign": bool(
                        project
                        and owner
                        and project != owner
                        and not _is_shared(change.path, shared)
                    ),
                    "reverted_to_blob": change.new_blob,
                    "stale_blob_first_seen_commit": stale_from,
                    "superseded_blob": change.old_blob,
                    "good_commit": good_commit,
                    "lines_lost": _deletions(repo, parent, sha, change.path),
                }
            )

    summary = Counter(finding["kind"] for finding in findings)
    return {
        "repo": repo,
        "rev_range": effective_range,
        "requested_rev_range": rev_range,
        "project_root": project_root,
        "shared_paths": list(shared),
        "commits_scanned": len(commits),
        "findings": findings,
        "summary": {
            "wide_commit": summary.get("wide_commit", 0),
            "stale_revert": summary.get("stale_revert", 0),
            "foreign_delete": summary.get("foreign_delete", 0),
            "total": len(findings),
        },
        "bounds": {
            "max_commits": MAX_COMMITS,
            "commits_truncated": commits_truncated,
            "max_paths_per_commit": MAX_PATHS_PER_COMMIT,
            "commits_with_paths_truncated": paths_truncated,
            "history_lookups": history_lookups,
            "history_truncated": history_truncated,
            "findings_truncated": findings_truncated,
        },
        "authority": (
            "static git history only; a finding is a candidate for supervisor "
            "review, not proof of intent"
        ),
    }


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------

_IN_PROGRESS = (
    "MERGE_HEAD",
    "REBASE_HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "index.lock",
)


def _blob_at(repo: str, rev: str, path: str) -> str | None:
    result = _git(
        repo, ["rev-parse", "--verify", "--quiet", f"{rev}:{path}"], check=False
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_file_at(repo: str, rev: str, path: str) -> bool:
    result = _git(repo, ["cat-file", "-t", f"{rev}:{path}"], check=False)
    return result.returncode == 0 and result.stdout.strip() == "blob"


def _index_blob(repo: str, path: str) -> str | None:
    result = _git(repo, ["ls-files", "-s", "--", path], check=False)
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    fields = line[0].split()
    return fields[1] if len(fields) >= 2 else None


def _restore(
    policy: SupervisorPolicy,
    repo: str,
    paths: list[str],
    source_commit: str,
    message: str,
    commit: bool,
) -> dict[str, Any]:
    screen_path_write(policy, repo)
    for path in paths:
        target = os.path.realpath(os.path.join(repo, path))
        screen_path_write(policy, target)
        screen_path_write(policy, os.path.join(repo, path))
        if not target.startswith(repo.rstrip("/") + "/"):
            raise GuardrailViolation(
                "path_outside_repo", f"{path!r} resolves outside the repository"
            )

    git_dir = _git(repo, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    screen_path_write(policy, git_dir)
    busy = [
        name for name in _IN_PROGRESS if os.path.exists(os.path.join(git_dir, name))
    ]
    if busy:
        raise GuardrailViolation(
            "repo_busy",
            f"repository has an operation in progress or a lock: {busy}; "
            "the owner must finish or abort it first",
        )

    resolved = _git(
        repo,
        ["rev-parse", "--verify", "--quiet", f"{source_commit}^{{commit}}"],
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise GuardrailViolation(
            "invalid_source_commit", f"{source_commit!r} is not a commit in this repo"
        )
    source = resolved.stdout.strip()

    missing = [path for path in paths if not _is_file_at(repo, source, path)]
    if missing:
        raise GuardrailViolation(
            "path_not_in_source",
            f"these paths do not exist as files in {source[:12]}: {missing}",
        )

    head = _git(repo, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
    before = {
        path: {
            "head_blob": _blob_at(repo, "HEAD", path),
            "index_blob": _index_blob(repo, path),
            "source_blob": _blob_at(repo, source, path),
        }
        for path in paths
    }

    _git(
        repo,
        [
            "restore",
            f"--source={source}",
            "--staged",
            "--worktree",
            "--",
            *paths,
        ],
        read_only=False,
    )

    changed = [
        path
        for path in paths
        if before[path]["head_blob"] != before[path]["source_blob"]
    ]
    result: dict[str, Any] = {
        "repo": repo,
        "source_commit": source,
        "previous_head": head,
        "restored_paths": paths,
        "changed_vs_head": changed,
        "before": before,
        "pushed": False,
    }

    if not commit:
        result["state"] = "restored_staged"
        result["new_commit"] = None
    elif not changed:
        result["state"] = "no_change"
        result["new_commit"] = None
    else:
        committed = _git(
            repo,
            ["commit", "-m", message, "--", *paths],
            check=False,
            read_only=False,
            timeout=300,
        )
        output = redact(policy, _truncate(committed.stdout + committed.stderr))
        if committed.returncode != 0:
            result["state"] = "commit_rejected"
            result["new_commit"] = None
            result["commit_output"] = output
            result["note"] = (
                "The commit (or a hook) refused. The restore is left staged; "
                "hooks were not bypassed."
            )
        else:
            new_head = _git(repo, ["rev-parse", "--verify", "HEAD"]).stdout.strip()
            touched = _git(
                repo,
                ["diff-tree", "-r", "--no-commit-id", "--name-only", "-z", new_head],
            ).stdout.split("\x00")
            touched = [item for item in touched if item]
            result["state"] = "committed"
            result["new_commit"] = new_head
            result["commit_paths"] = touched
            result["commit_output"] = output
            unexpected = sorted(set(touched) - set(paths))
            if unexpected:
                result["warning"] = f"commit touched unexpected paths: {unexpected}"

    result["after"] = {
        path: {
            "head_blob": _blob_at(repo, "HEAD", path),
            "index_blob": _index_blob(repo, path),
        }
        for path in paths
    }
    return result


# --------------------------------------------------------------------------
# Tool surface
# --------------------------------------------------------------------------


class ButlerRepoForensics:
    """Audit a shared research checkout and restore exact paths."""

    def __init__(
        self, policy_loader: Callable[[], SupervisorPolicy] = load_policy
    ) -> None:
        self.policy_loader = policy_loader

    def _audit(self, policy: SupervisorPolicy, record: dict[str, Any]) -> None:
        try:
            write_audit(policy, record)
        except OSError as exc:
            # A failed audit write must not hide the original outcome, but the
            # missing audit line is itself a fault the operator must see.
            record_caught_exception(
                "butler",
                "butler.supervisor.audit_write_failed",
                "Butler could not write a repository forensics audit record.",
                exc,
                stage="repo-forensics",
            )

    async def repo_audit(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy_loader()
        base = {"tool": AUDIT_TOOL, "repo": arguments.get("repo")}
        try:
            policy.require_enabled()
            repo = _resolve_repo(policy, arguments.get("repo"))
            default_range = arguments.get("rev_range") in (None, "")
            rev_range = _validate_range(
                DEFAULT_RANGE if default_range else arguments.get("rev_range")
            )
            project_root = _validate_project_root(arguments.get("project_root"))
            shared = _validate_shared(arguments.get("shared_paths"))
            report = await asyncio.to_thread(
                _audit_repo, repo, rev_range, project_root, shared, default_range
            )
        except (SupervisorDisabled, GuardrailViolation, GitError) as exc:
            self._audit(
                policy,
                {
                    **base,
                    "rev_range": arguments.get("rev_range"),
                    "outcome": "refused"
                    if isinstance(exc, PermissionError)
                    else "error",
                    "rule": getattr(exc, "rule", type(exc).__name__),
                    "detail": str(exc),
                },
            )
            raise
        self._audit(
            policy,
            {
                **base,
                "repo": repo,
                "rev_range": report["rev_range"],
                "outcome": "ok",
                "commits_scanned": report["commits_scanned"],
                "counts": report["summary"],
            },
        )
        return report

    async def repo_restore(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self.policy_loader()
        base = {"tool": RESTORE_TOOL, "repo": arguments.get("repo")}
        try:
            policy.require_enabled()
            repo = _resolve_repo(policy, arguments.get("repo"))
            raw_paths = arguments.get("paths")
            if not isinstance(raw_paths, list) or not (
                1 <= len(raw_paths) <= MAX_RESTORE_PATHS
            ):
                raise GuardrailViolation(
                    "invalid_paths", f"paths must list 1..{MAX_RESTORE_PATHS} files"
                )
            paths = list(dict.fromkeys(_validate_restore_path(p) for p in raw_paths))
            source_commit = arguments.get("source_commit")
            if not isinstance(source_commit, str) or not _HEX_COMMIT.fullmatch(
                source_commit
            ):
                raise GuardrailViolation(
                    "invalid_source_commit",
                    "source_commit must be a 7..40 character hex commit id",
                )
            message = arguments.get("message")
            if not isinstance(message, str) or not message.strip() or "\x00" in message:
                raise GuardrailViolation(
                    "invalid_message", "a non-empty commit message is required"
                )
            commit = arguments.get("commit", True)
            if not isinstance(commit, bool):
                raise GuardrailViolation("invalid_commit", "commit must be a boolean")
            result = await asyncio.to_thread(
                _restore, policy, repo, paths, source_commit, message, commit
            )
        except (SupervisorDisabled, GuardrailViolation, GitError) as exc:
            self._audit(
                policy,
                {
                    **base,
                    "paths": arguments.get("paths"),
                    "source_commit": arguments.get("source_commit"),
                    "outcome": "refused"
                    if isinstance(exc, PermissionError)
                    else "error",
                    "rule": getattr(exc, "rule", type(exc).__name__),
                    "detail": str(exc),
                },
            )
            raise
        self._audit(
            policy,
            {
                **base,
                "repo": repo,
                "outcome": result["state"],
                "paths": paths,
                "source_commit": result["source_commit"],
                "previous_head": result["previous_head"],
                "new_commit": result.get("new_commit"),
                "before": result["before"],
                "after": result["after"],
            },
        )
        return result

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name=AUDIT_TOOL,
                    description=(
                        "Audit a commit range of an allowlisted shared research "
                        "checkout for cross-project damage. Each non-merge commit's "
                        f"owner is its '{TRAILER_KEY}:' trailer, else the project "
                        "directory with the most changed paths. Reports wide_commit "
                        "(paths in another project's directory), stale_revert (a "
                        "path written back to an older, superseded blob, with the "
                        "good commit and lines lost) and foreign_delete. Read-only; "
                        "findings are candidates for supervisor review."
                    ),
                    input_schema=_AUDIT_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.LOCAL_READ,
                    filesystem_access="read",
                    timeout_seconds=900,
                    idempotency=IdempotencyBehavior.SAFE,
                ),
                self.repo_audit,
            ),
            AnalysisTool(
                ToolSpec(
                    name=RESTORE_TOOL,
                    description=(
                        "Restore exact repo-relative file paths in an allowlisted "
                        "shared checkout to their content at one known-good commit, "
                        "then commit only those paths with the repository's hooks. "
                        "Refuses globs, '..', absolute paths, the pull-only "
                        "production mirror, and repos with a merge, rebase or index "
                        "lock in progress. Never pushes; never bypasses hooks. A "
                        "hook rejection leaves the restore staged and returns the "
                        "hook output."
                    ),
                    input_schema=_RESTORE_INPUT,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    filesystem_access="workspace_write",
                    timeout_seconds=600,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.repo_restore,
            ),
        ]
