"""Work check-in fabrication guard: phantom commit hashes and forged sign-offs."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from pathlib import Path
import shutil
import subprocess
import sys
import types
from collections.abc import Sequence

import pytest

import nebula.v3.checkin_fabrication_guard as guard
from nebula.v3.domain import Engagement, ScopePolicy
from nebula.v3.storage import NebulaStore
from nebula.v3.tools import ToolInvocation
from nebula.v3.work import WorkCheckIn, WorkCreate, WorkService, work_components

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "HOME": str(repo),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    (path / "file.txt").write_text("one\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-q", "-m", "first")
    return path, _git(path, "rev-parse", "HEAD")


PHANTOM = "0123456789abcdef0123456789abcdef01234567"


def _env(repos: dict[str, list[str]], mode: str = "flag") -> dict[str, str]:
    return {guard.REPOS_ENV: json.dumps(repos), guard.MODE_ENV: mode}


# --- extraction ---------------------------------------------------------------


def test_extracts_12_and_40_char_lowercase_hashes() -> None:
    text = f"Landed abc123def456 and {PHANTOM}; also abc123def456 again."
    assert guard.extract_commit_hashes(text) == ["abc123def456", PHANTOM]


def test_ignores_uuid_fragments() -> None:
    text = "run 123e4567-e89b-12d3-a456-426614174000 and a1b2c3d4e5f6-0000"
    assert guard.extract_commit_hashes(text) == []


def test_ignores_uppercase_and_mixed_case() -> None:
    assert guard.extract_commit_hashes("ABCDEF123456 and AbCdEf123456") == []


def test_ignores_hex_inside_longer_runs() -> None:
    digest = "ab" * 32  # 64 hex characters
    text = f"{digest} and abc123def4567 and Xabc123def456 and abc123def456Q"
    assert guard.extract_commit_hashes(text) == []


def test_ignores_all_digit_all_letter_and_digest_prefixes() -> None:
    text = "id 123456789012, word abcdefabcdef, sha256: abc123def456, md5=fe12ab34cd56"
    assert guard.extract_commit_hashes(text) == []


def test_other_lengths_are_not_checked() -> None:
    assert guard.extract_commit_hashes("short abc1234 and 13 abc123def4567") == []


# --- resolution ---------------------------------------------------------------


@needs_git
def test_real_hash_resolves_and_phantom_does_not(repo) -> None:
    path, head = repo
    text = f"Committed {head} and {head[:12]} and {PHANTOM}."
    assert guard.find_phantom_hashes(text, [path]) == [PHANTOM]


@needs_git
def test_hash_real_in_any_repo_is_real(repo, tmp_path) -> None:
    path, head = repo
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    assert guard.find_phantom_hashes(f"see {head}", [other, path]) == []


@needs_git
def test_non_repository_leaves_hashes_unverified(tmp_path) -> None:
    result = guard.GuardResult(mode="flag")
    phantoms = guard.find_phantom_hashes(
        f"see {PHANTOM}", [tmp_path / "missing"], result=result
    )
    assert phantoms == []
    assert result.unverified_hashes == [PHANTOM]
    assert result.errors


def test_git_missing_or_timeout_never_raises() -> None:
    def broken(repo: Path, hashes: Sequence[str], timeout: float) -> dict[str, bool]:
        raise subprocess.TimeoutExpired(["git"], timeout)

    result = guard.check_text(
        "p1", f"see {PHANTOM}", environ=_env({"p1": ["/nowhere"]}), runner=broken
    )
    assert result.phantom_hashes == []
    assert result.unverified_hashes == [PHANTOM]


def test_hash_cap_is_reported() -> None:
    hashes = [f"{index:04d}abcdef01" for index in range(5)]
    seen: list[list[str]] = []

    def runner(repo: Path, values: Sequence[str], timeout: float) -> dict[str, bool]:
        seen.append(list(values))
        return {value: True for value in values}

    result = guard.GuardResult(mode="flag")
    guard.find_phantom_hashes(
        " ".join(hashes), ["/repo"], max_hashes=3, runner=runner, result=result
    )
    assert seen == [hashes[:3]]
    assert result.unchecked_hash_count == 2


# --- configuration --------------------------------------------------------------


def test_mode_and_repo_configuration() -> None:
    assert guard.guard_mode({}) == "flag"
    assert guard.guard_mode({guard.MODE_ENV: "REJECT"}) == "reject"
    assert guard.guard_mode({guard.MODE_ENV: "bogus"}) == "flag"
    env = {guard.REPOS_ENV: json.dumps({"p1": ["/a"], "*": "/b"})}
    assert guard.configured_repos("p1", env) == [Path("/a")]
    assert guard.configured_repos("p2", env) == [Path("/b")]
    assert guard.configured_repos("p1", {guard.REPOS_ENV: "{not json"}) == []


def test_no_repo_configured_skips_and_says_so() -> None:
    result = guard.check_text("p1", f"see {PHANTOM}", environ={})
    assert result.phantom_hashes == []
    assert result.skipped_reason and guard.REPOS_ENV in result.skipped_reason


def test_off_mode_checks_nothing() -> None:
    result = guard.check_text(
        "p1", f"see {PHANTOM}", environ=_env({"p1": ["/x"]}, mode="off")
    )
    assert not result.flagged and result.skipped_reason == "guard mode is off"


# --- sign-off detection -------------------------------------------------------------


def test_signoff_check_unavailable_without_review_gating(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "nebula.v3.butler_review_gating", None)
    assert guard.detect_signoffs("Reviewer approved.") == ([], "unavailable")


def test_signoff_detector_is_used_and_its_failure_is_contained(monkeypatch) -> None:
    module = types.ModuleType("nebula.v3.butler_review_gating")
    module.detect_forged_signoff = lambda text: (  # type: ignore[attr-defined]
        ["independent reviewer approved"] if "approved" in text else []
    )
    monkeypatch.setitem(sys.modules, "nebula.v3.butler_review_gating", module)
    assert guard.detect_signoffs("independent reviewer approved") == (
        ["independent reviewer approved"],
        "checked",
    )

    def explode(text: str) -> list[str]:
        raise RuntimeError("detector bug")

    module.detect_forged_signoff = explode  # type: ignore[attr-defined]
    assert guard.detect_signoffs("x") == ([], "failed")


# --- Work check-in integration ---------------------------------------------------------


async def _item(tmp_path: Path) -> tuple[WorkService, str, str]:
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Guarded project"))
    service = WorkService(store)
    item = await service.create(
        project.id, WorkCreate(title="Ship it"), actor_id="operator"
    )
    return service, project.id, item.id


@needs_git
async def test_flag_mode_annotates_agent_check_in(tmp_path, repo, monkeypatch) -> None:
    path, head = repo
    service, project_id, item_id = await _item(tmp_path)
    monkeypatch.setenv(guard.REPOS_ENV, json.dumps({project_id: [str(path)]}))
    monkeypatch.setenv(guard.MODE_ENV, "flag")
    update, result = await service.check_in_guarded(
        project_id,
        item_id,
        WorkCheckIn(summary=f"Pushed {head} and {PHANTOM}."),
        actor_kind="agent",
        actor_id="agent-1",
    )
    assert result is not None and result.phantom_hashes == [PHANTOM]
    assert "fabrication guard" in update.summary
    assert PHANTOM in update.summary.split("\n\n")[-1]
    assert head not in update.summary.split("\n\n")[-1]


@needs_git
async def test_flag_mode_keeps_summary_within_bound(
    tmp_path, repo, monkeypatch
) -> None:
    path, _head = repo
    service, project_id, item_id = await _item(tmp_path)
    monkeypatch.setenv(guard.REPOS_ENV, json.dumps({project_id: [str(path)]}))
    update = await service.check_in(
        project_id,
        item_id,
        WorkCheckIn(summary=f"{PHANTOM} " + "x" * 3_950),
        actor_kind="agent",
        actor_id="agent-1",
    )
    assert len(update.summary) <= 4_000
    assert update.summary.endswith("]")


@needs_git
async def test_reject_mode_refuses_and_names_hashes(
    tmp_path, repo, monkeypatch
) -> None:
    path, _head = repo
    service, project_id, item_id = await _item(tmp_path)
    monkeypatch.setenv(guard.REPOS_ENV, json.dumps({project_id: [str(path)]}))
    monkeypatch.setenv(guard.MODE_ENV, "reject")
    with pytest.raises(guard.FabricationRejected, match=PHANTOM):
        await service.check_in(
            project_id,
            item_id,
            WorkCheckIn(summary="done", next_step=f"verify {PHANTOM}"),
            actor_kind="agent",
            actor_id="agent-1",
        )
    assert await service.updates(project_id, item_id) == []


async def test_operator_check_ins_are_not_guarded(tmp_path, monkeypatch) -> None:
    service, project_id, item_id = await _item(tmp_path)
    monkeypatch.setenv(guard.REPOS_ENV, json.dumps({"*": ["/nowhere"]}))
    monkeypatch.setenv(guard.MODE_ENV, "reject")
    update, result = await service.check_in_guarded(
        project_id,
        item_id,
        WorkCheckIn(summary=f"see {PHANTOM}"),
        actor_kind="operator",
        actor_id="operator",
    )
    assert result is None and update.summary == f"see {PHANTOM}"


async def test_guard_crash_does_not_block_check_in(tmp_path, monkeypatch) -> None:
    service, project_id, item_id = await _item(tmp_path)

    def crash(*args, **kwargs):
        raise RuntimeError("guard bug")

    monkeypatch.setattr("nebula.v3.work.check_text", crash)
    update, result = await service.check_in_guarded(
        project_id,
        item_id,
        WorkCheckIn(summary=f"see {PHANTOM}"),
        actor_kind="agent",
        actor_id="agent-1",
    )
    assert result is None and update.summary == f"see {PHANTOM}"


async def test_work_tool_reports_guard_result(tmp_path, monkeypatch) -> None:
    service, project_id, item_id = await _item(tmp_path)
    monkeypatch.delenv(guard.REPOS_ENV, raising=False)
    monkeypatch.setenv(guard.MODE_ENV, "flag")
    components = work_components(
        service,
        project_id,
        tmp_path,
        ScopePolicy(id=f"scope:{project_id}", engagement_id=project_id),
    )
    result = await components.broker.execute(
        ToolInvocation(
            engagement_id=project_id,
            run_id="run-1",
            tool_name="work_check_in",
            arguments={"item_id": item_id, "summary": f"see {PHANTOM}"},
            workspace=tmp_path,
        ),
        components.scope,
    )
    report = result.output["fabrication_guard"]
    assert report["phantom_hashes"] == []
    assert guard.REPOS_ENV in report["skipped_reason"]
