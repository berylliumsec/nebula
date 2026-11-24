"""Repository forensics and exact-path restore for Butler supervisor powers."""

import json
import subprocess
from pathlib import Path

import pytest

from nebula.v3.butler_repo_forensics import (
    AUDIT_TOOL,
    RESTORE_TOOL,
    ButlerRepoForensics,
)
from nebula.v3.butler_supervisor_policy import GuardrailViolation, SupervisorDisabled


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _git(repo: Path, *args: str, strip: bool = True) -> str:
    output = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return output.strip() if strip else output


def _commit(repo: Path, message: str, *paths: str) -> str:
    if paths:
        _git(repo, "add", "--", *paths)
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, text: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return rel


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "shared"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.invalid")
    protected = tmp_path / "prod"
    protected.mkdir()
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", "1")
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_FORENSICS_REPOS", str(repo))
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG", str(audit))
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS", str(protected))
    return {"repo": repo, "audit": audit, "protected": protected, "tmp": tmp_path}


def _audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _of(report: dict, kind: str) -> list[dict]:
    return [item for item in report["findings"] if item["kind"] == kind]


async def test_tool_specs_are_registered():
    actions = ButlerRepoForensics().actions()
    assert [a.spec.name for a in actions] == [AUDIT_TOOL, RESTORE_TOOL]


async def test_kill_switch_refuses(env, monkeypatch):
    monkeypatch.setenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", "0")
    tool = ButlerRepoForensics()
    with pytest.raises(SupervisorDisabled):
        await tool.repo_audit({"repo": str(env["repo"])})
    with pytest.raises(SupervisorDisabled):
        await tool.repo_restore(
            {
                "repo": str(env["repo"]),
                "paths": ["a"],
                "source_commit": "abcdef1",
                "message": "m",
            }
        )


async def test_non_allowlisted_repo_refused(env):
    other = env["tmp"] / "other"
    other.mkdir()
    _git(other, "init", "-q")
    with pytest.raises(GuardrailViolation) as caught:
        await ButlerRepoForensics().repo_audit({"repo": str(other)})
    assert caught.value.rule == "repo_not_allowlisted"


async def test_bad_rev_range_refused(env):
    repo = env["repo"]
    _commit(repo, "init", _write(repo, "projects/a/x.txt", "1\n"))
    with pytest.raises(GuardrailViolation) as caught:
        await ButlerRepoForensics().repo_audit(
            {"repo": str(repo), "rev_range": "--output=/tmp/x"}
        )
    assert caught.value.rule == "invalid_rev_range"


async def test_wide_commit_and_foreign_delete_detected(env):
    repo = env["repo"]
    _write(repo, "projects/alpha/a1.txt", "a1\n")
    _write(repo, "projects/beta/b1.txt", "b1\n")
    _write(repo, "projects/beta/b2.txt", "b2\n")
    _write(repo, "ledger/index.json", "{}\n")
    base = _commit(repo, "seed", ".")
    # alpha's commit also edits beta/b1 and deletes beta/b2 and the shared ledger.
    _write(repo, "projects/alpha/a1.txt", "a1 v2\n")
    _write(repo, "projects/alpha/a2.txt", "a2\n")
    _write(repo, "projects/beta/b1.txt", "b1 clobbered\n")
    (repo / "projects/beta/b2.txt").unlink()
    _write(repo, "ledger/index.json", '{"x": 1}\n')
    wide = _commit(repo, "alpha work", ".")

    report = await ButlerRepoForensics().repo_audit(
        {
            "repo": str(repo),
            "rev_range": f"{base}..HEAD",
            "shared_paths": ["ledger/*"],
        }
    )
    assert report["commits_scanned"] == 1
    [finding] = _of(report, "wide_commit")
    assert finding["commit"] == wide
    assert finding["owner_project"] == "alpha"
    assert finding["owner_source"] == "most_changed_paths"
    assert sorted(finding["foreign_paths"]) == [
        "projects/beta/b1.txt",
        "projects/beta/b2.txt",
    ]
    [deleted] = _of(report, "foreign_delete")
    assert deleted["path"] == "projects/beta/b2.txt"
    assert deleted["project"] == "beta"
    assert report["summary"]["wide_commit"] == 1
    assert report["summary"]["foreign_delete"] == 1


async def test_default_range_works_on_short_history(env):
    repo = env["repo"]
    _commit(repo, "init", _write(repo, "projects/a/x.txt", "1\n"))
    report = await ButlerRepoForensics().repo_audit({"repo": str(repo)})
    assert report["rev_range"] == "HEAD"
    assert report["summary"]["total"] == 0


async def test_stale_revert_detected_with_good_commit_and_lines_lost(env):
    repo = env["repo"]
    path = "projects/gamma/notes.md"
    old_text = "line\n" * 10
    _write(repo, path, old_text)
    _commit(repo, "gamma v1", path)
    new_text = old_text + "".join(f"added {i}\n" for i in range(40))
    _write(repo, path, new_text)
    good = _commit(repo, "gamma v2", path)
    # Goal DELTA commits its own file plus a stale copy of gamma's notes.
    _write(repo, "projects/delta/work.txt", "delta\n")
    _write(repo, "projects/delta/more.txt", "delta\n")
    _write(repo, path, old_text)
    bad = _commit(
        repo,
        "delta work\n\nNebula-Project: delta",
        "projects/delta/work.txt",
        "projects/delta/more.txt",
        path,
    )

    report = await ButlerRepoForensics().repo_audit(
        {"repo": str(repo), "rev_range": f"{good}..HEAD"}
    )
    [stale] = _of(report, "stale_revert")
    assert stale["commit"] == bad
    assert stale["path"] == path
    assert stale["good_commit"] == good
    assert stale["lines_lost"] == 40
    assert stale["foreign"] is True
    assert stale["reverted_to_blob"] == _git(repo, "rev-parse", f"{bad}:{path}")
    assert stale["superseded_blob"] == _git(repo, "rev-parse", f"{good}:{path}")
    [wide] = _of(report, "wide_commit")
    assert wide["foreign_paths"] == [path]


async def test_ordinary_edit_is_not_stale_revert(env):
    repo = env["repo"]
    path = "projects/a/x.txt"
    _commit(repo, "v1", _write(repo, path, "1\n"))
    base = _commit(repo, "v2", _write(repo, path, "2\n"))
    _commit(repo, "v3", _write(repo, path, "3\n"))
    report = await ButlerRepoForensics().repo_audit(
        {"repo": str(repo), "rev_range": f"{base}..HEAD"}
    )
    assert report["findings"] == []


async def test_trailer_ownership_honoured(env):
    repo = env["repo"]
    _write(repo, "projects/alpha/a.txt", "a\n")
    base = _commit(repo, "seed", ".")
    # Most changed paths are in beta, but the trailer says alpha owns it.
    _write(repo, "projects/alpha/a.txt", "a2\n")
    for name in ("b1", "b2", "b3"):
        _write(repo, f"projects/beta/{name}.txt", name)
    _commit(repo, "alpha sweep\n\nNebula-Project: alpha", ".")
    report = await ButlerRepoForensics().repo_audit(
        {"repo": str(repo), "rev_range": f"{base}..HEAD"}
    )
    [wide] = _of(report, "wide_commit")
    assert wide["owner_project"] == "alpha"
    assert wide["owner_source"] == "trailer"
    assert sorted(wide["foreign_paths"]) == [
        "projects/beta/b1.txt",
        "projects/beta/b2.txt",
        "projects/beta/b3.txt",
    ]


async def test_restore_exact_path_commits_only_that_path(env):
    repo = env["repo"]
    target = "projects/gamma/notes.md"
    _write(repo, target, "good\n" * 5)
    _write(repo, "projects/other/o.txt", "o\n")
    good = _commit(repo, "seed", ".")
    _write(repo, target, "stale\n")
    _commit(repo, "clobber", target)
    # Other goals' uncommitted work must stay untouched.
    _write(repo, "projects/other/o.txt", "dirty worktree\n")
    _write(repo, "projects/other/staged.txt", "staged\n")
    _git(repo, "add", "projects/other/staged.txt")

    result = await ButlerRepoForensics().repo_restore(
        {
            "repo": str(repo),
            "paths": [target],
            "source_commit": good[:12],
            "message": "Restore gamma notes",
        }
    )
    assert result["state"] == "committed"
    assert result["pushed"] is False
    assert result["source_commit"] == good
    assert result["commit_paths"] == [target]
    assert (repo / target).read_text() == "good\n" * 5
    assert result["new_commit"] == _git(repo, "rev-parse", "HEAD")
    assert result["after"][target]["head_blob"] == _git(
        repo, "rev-parse", f"{good}:{target}"
    )
    assert result["before"][target]["head_blob"] != result["after"][target]["head_blob"]
    assert (repo / "projects/other/o.txt").read_text() == "dirty worktree\n"
    status = _git(repo, "status", "--porcelain", strip=False)
    assert "A  projects/other/staged.txt" in status
    assert " M projects/other/o.txt" in status

    lines = _audit_lines(env["audit"])
    assert lines[-1]["tool"] == RESTORE_TOOL
    assert lines[-1]["outcome"] == "committed"
    assert lines[-1]["new_commit"] == result["new_commit"]


async def test_restore_hook_rejection_leaves_staged(env):
    repo = env["repo"]
    target = "projects/a/x.txt"
    good = _commit(repo, "v1", _write(repo, target, "good\n"))
    _commit(repo, "v2", _write(repo, target, "bad\n"))
    hook = repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho 'hook says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    result = await ButlerRepoForensics().repo_restore(
        {
            "repo": str(repo),
            "paths": [target],
            "source_commit": good,
            "message": "restore",
        }
    )
    assert result["state"] == "commit_rejected"
    assert "hook says no" in result["commit_output"]
    assert _git(repo, "status", "--porcelain") == f"M  {target}"


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.txt", "/etc/passwd", "projects/a/../b.txt", "projects/*", "-rf"],
)
async def test_restore_refuses_unsafe_paths(env, bad_path):
    repo = env["repo"]
    good = _commit(repo, "v1", _write(repo, "projects/a/x.txt", "x\n"))
    with pytest.raises(GuardrailViolation) as caught:
        await ButlerRepoForensics().repo_restore(
            {
                "repo": str(repo),
                "paths": [bad_path],
                "source_commit": good,
                "message": "m",
            }
        )
    assert caught.value.rule == "invalid_path"
    assert _audit_lines(env["audit"])[-1]["outcome"] == "refused"


async def test_restore_inside_protected_path_refused(env, monkeypatch):
    repo = env["repo"]
    good = _commit(repo, "v1", _write(repo, "projects/a/x.txt", "x\n"))
    monkeypatch.setenv(
        "NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS", str(repo / "projects/a")
    )
    with pytest.raises(GuardrailViolation) as caught:
        await ButlerRepoForensics().repo_restore(
            {
                "repo": str(repo),
                "paths": ["projects/a/x.txt"],
                "source_commit": good,
                "message": "m",
            }
        )
    assert caught.value.rule == "production_mirror_read_only"


async def test_restore_refuses_when_index_locked(env):
    repo = env["repo"]
    good = _commit(repo, "v1", _write(repo, "projects/a/x.txt", "x\n"))
    (repo / ".git/index.lock").write_text("")
    with pytest.raises(GuardrailViolation) as caught:
        await ButlerRepoForensics().repo_restore(
            {
                "repo": str(repo),
                "paths": ["projects/a/x.txt"],
                "source_commit": good,
                "message": "m",
            }
        )
    assert caught.value.rule == "repo_busy"


async def test_audit_log_written(env):
    repo = env["repo"]
    _commit(repo, "init", _write(repo, "projects/a/x.txt", "1\n"))
    await ButlerRepoForensics().repo_audit({"repo": str(repo), "rev_range": "HEAD"})
    [line] = _audit_lines(env["audit"])
    assert line["tool"] == AUDIT_TOOL
    assert line["outcome"] == "ok"
    assert line["rev_range"] == "HEAD"
    assert line["counts"]["total"] == 0
    assert line["schema"] == "nebula.butler-supervisor-audit/v1"
