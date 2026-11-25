"""Independent review requested by a harness-backed Butler at Act.

Reproduces a refused review: the reviewer ran on a Claude harness profile,
the goal ran on a different provider model, and Butler cited evidence as the
project-relative paths its workspace tools return. Core had no
``NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS``, so the evidence reader raised a
``GuardrailViolation`` that reached Butler only as a bare
``permission_denied``. The goal's own project workspace is now an evidence
root, relative paths resolve there, and every refusal names Core's rule.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from nebula.v3 import butler_independent_review as review_module
from nebula.v3.butler_independent_review import (
    REVIEW_KEY,
    ButlerIndependentReview,
    ReviewUnavailable,
    review_refusal,
)
from nebula.v3.butler_supervisor_policy import GuardrailViolation, load_policy
from nebula.v3.domain import GOAL_COMPLETION_PROPOSAL_KEY, HarnessProfile
from nebula.v3.tool_failures import tool_failure
from nebula.v3.tools import GuidedToolError
from tests.v3.test_butler_harness_parity import CLAUDE_MODEL, _HarnessReviewStore
from tests.v3.test_butler_supervisor_powers import _env, _FakeWork, _goal


RELATIVE = ["runtime/case-1/DISPOSITION.md", "runtime/case-1/static/layout.md"]


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "project-workspace"
    (root / "runtime/case-1/static").mkdir(parents=True)
    (root / "runtime/case-1/DISPOSITION.md").write_text(
        "decisive fact: the service refuses external callers\n"
    )
    (root / "runtime/case-1/static/layout.md").write_text("layout notes\n")
    return root


def _service(
    tmp_path: Path,
    *,
    workspace: Path | None,
    session_model: str = "vendor/goal-model-1",
    resolver_error: BaseException | None = None,
    **extra_env: str,
):
    """A Claude-harness reviewer with no evidence roots, as in production."""

    goal = _goal()
    # A goal that completed itself holds no proposal evidence of its own.
    goal.metadata[GOAL_COMPLETION_PROPOSAL_KEY]["completion_evidence"] = []
    work = _FakeWork(goal)
    store = _HarnessReviewStore(work, session_model)
    prompts: list[str] = []
    resolved: list[str] = []

    async def runner(profile: HarnessProfile, model: str, prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "verdict": "NOT_READY",
                "summary": "upgrade paths untested",
                "must_do": ["test the remaining client contexts"],
            }
        )

    async def resolver(project_id: str) -> Path:
        resolved.append(project_id)
        if resolver_error is not None:
            raise resolver_error
        assert workspace is not None
        return workspace

    env = _env(
        tmp_path,
        NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID="claude-reviewer",
        **extra_env,
    )
    service = ButlerIndependentReview(
        store,
        work,
        "p1",
        policy_loader=lambda: load_policy(env),
        provider_factory=lambda _profile: pytest.fail("no provider is used"),
        harness_runner=runner,
        workspace_resolver=(
            resolver if workspace is not None or resolver_error else None
        ),
    )
    return service, work, prompts, resolved


def _arguments(paths: list[str]) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "evidence_paths": paths,
        "apply": True,
        "on_residuals": "feed",
    }


def _envelope(service: ButlerIndependentReview, error: BaseException) -> dict:
    [tool] = service.actions()
    return tool_failure(tool.spec, _arguments(RELATIVE), error, phase="execute")


@pytest.mark.anyio
async def test_production_shape_without_a_workspace_is_a_named_refusal(
    tmp_path: Path,
) -> None:
    service, work, prompts, _ = _service(tmp_path, workspace=None)

    # The trigger: the raw review refuses on its evidence guardrail.
    with pytest.raises(GuardrailViolation) as raw:
        await service.review("s1", RELATIVE)
    assert raw.value.rule == "not_configured"

    # Butler now sees the exact rule and remedy instead of a bare denial.
    with pytest.raises(GuidedToolError) as caught:
        await service.request(_arguments(RELATIVE))
    assert caught.value.code == "independent_review.not_configured"
    envelope = _envelope(service, caught.value)
    assert envelope["category"] == "permission_denied"
    assert envelope["error_code"] == "independent_review.not_configured"
    assert "NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS" in envelope["problem"]
    assert "workspace" in envelope["next_action"]
    assert envelope["invalid_input"] == "evidence_paths"
    assert envelope["side_effects"] == "none"
    assert prompts == []
    assert REVIEW_KEY not in work.goals.goal.metadata


@pytest.mark.anyio
async def test_harness_butler_reviews_with_project_relative_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    service, work, prompts, resolved = _service(tmp_path, workspace=workspace)

    result = await service.request(_arguments(RELATIVE))

    assert resolved == ["p1"]
    [prompt] = prompts
    assert "the service refuses external callers" in prompt
    assert "layout notes" in prompt
    record = work.goals.goal.metadata[REVIEW_KEY]
    assert record["verdict"] == "NOT_READY"
    assert record["evidence_paths"] == RELATIVE
    assert record["reviewer_harness_profile_id"] == "claude-reviewer"
    assert record["reviewer_model"] == CLAUDE_MODEL
    assert record["reviewer_tools"] == []
    assert result["review"]["verdict"] == "NOT_READY"


@pytest.mark.anyio
async def test_absolute_path_inside_the_project_workspace_is_admitted(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    service, _, prompts, _ = _service(tmp_path, workspace=workspace)

    await service.request(
        _arguments([str(workspace / "runtime/case-1/DISPOSITION.md")])
    )

    assert "the service refuses external callers" in prompts[0]


@pytest.mark.parametrize(
    ("escape", "rule"),
    [
        ("../outside.md", "evidence_outside_roots"),
        # A sibling project's workspace is not this goal's evidence root.
        ("{sibling}/notes.md", "evidence_outside_roots"),
        # Links are never followed, even to a file outside the roots.
        ("runtime/link.md", "evidence_not_regular_file"),
        ("runtime/linked-dir/notes.md", "evidence_not_regular_file"),
        ("runtime/case-1", "evidence_not_regular_file"),
    ],
)
@pytest.mark.anyio
async def test_paths_escaping_the_workspace_are_refused(
    tmp_path: Path, escape: str, rule: str
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("private bytes\n")
    sibling = tmp_path / "sibling-project-workspace"
    sibling.mkdir()
    (sibling / "notes.md").write_text("private bytes\n")
    os.symlink(outside, workspace / "runtime/link.md")
    os.symlink(sibling, workspace / "runtime/linked-dir")
    escape = escape.format(sibling=sibling)
    service, _, prompts, _ = _service(tmp_path, workspace=workspace)

    with pytest.raises(GuidedToolError) as caught:
        await service.request(_arguments([escape]))

    assert caught.value.code == f"independent_review.{rule}"
    envelope = _envelope(service, caught.value)
    # Core-authored text only: the cited path is not echoed back.
    assert escape not in envelope["problem"]
    assert prompts == []


@pytest.mark.anyio
async def test_link_swapped_in_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("private bytes\n")
    target = workspace / RELATIVE[0]
    original = review_module._root_relative

    def check_then_swap(candidate, roots):
        # The path checks out as a regular file inside the workspace, then
        # the goal replaces it with a link before Core opens it.
        located = original(candidate, roots)
        target.unlink()
        os.symlink(outside, target)
        return located

    monkeypatch.setattr(review_module, "_root_relative", check_then_swap)
    service, _, prompts, _ = _service(tmp_path, workspace=workspace)

    with pytest.raises(GuidedToolError) as caught:
        await service.request(_arguments([RELATIVE[0]]))

    assert caught.value.code == "independent_review.evidence_not_regular_file"
    assert prompts == []


@pytest.mark.anyio
async def test_oversized_evidence_is_bounded_and_fully_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    data = b"evidence line\n" * (review_module.MAX_FILE_BYTES // 4)
    assert len(data) > 3 * review_module.MAX_FILE_BYTES
    (workspace / "runtime/big.log").write_bytes(data)
    read_sizes: list[int] = []
    real_read = os.read

    def recording_read(descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(review_module.os, "read", recording_read)
    service, _, prompts, _ = _service(tmp_path, workspace=workspace)

    files = review_module._read_evidence(
        load_policy(_env(tmp_path)), ["runtime/big.log"], str(workspace)
    )

    [entry] = files
    assert entry["truncated"] is True
    assert entry["bytes"] == len(data)
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(entry["content"]) == review_module.MAX_FILE_BYTES
    assert read_sizes and max(read_sizes) <= review_module._READ_CHUNK
    await service.request(_arguments(["runtime/big.log"]))
    assert len(prompts[0]) < len(data)


@pytest.mark.anyio
async def test_configured_roots_still_admit_absolute_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    extra = tmp_path / "shared-evidence"
    extra.mkdir()
    (extra / "note.md").write_text("shared fact\n")
    service, _, prompts, _ = _service(
        tmp_path,
        workspace=workspace,
        NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS=str(extra),
    )

    await service.request(_arguments([RELATIVE[0], str(extra / "note.md")]))

    assert "shared fact" in prompts[0]
    assert "the service refuses external callers" in prompts[0]


@pytest.mark.anyio
async def test_unresolvable_workspace_falls_back_to_configured_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        "nebula.v3.butler_independent_review.record_caught_exception",
        lambda _area, code, *args, **kwargs: recorded.append(code),
    )
    service, _, prompts, _ = _service(
        tmp_path, workspace=None, resolver_error=OSError("workspace missing")
    )

    with pytest.raises(GuidedToolError) as caught:
        await service.request(_arguments(RELATIVE))

    assert caught.value.code == "independent_review.not_configured"
    assert recorded == ["butler.independent_review.workspace_unavailable"]
    assert prompts == []


@pytest.mark.anyio
async def test_same_model_refusal_names_the_conflict_and_remedy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    service, _, prompts, _ = _service(
        tmp_path, workspace=workspace, session_model=CLAUDE_MODEL
    )

    with pytest.raises(GuidedToolError) as caught:
        await service.request(_arguments(RELATIVE))

    assert caught.value.code == "independent_review.reviewer_not_independent"
    envelope = _envelope(service, caught.value)
    assert envelope["category"] == "unavailable_dependency"
    assert "goal's own model" in envelope["problem"]
    assert "NEBULA_BUTLER_REVIEW_MODEL" in envelope["next_action"]
    assert envelope["side_effects"] == "none"
    assert prompts == []


def test_reviewer_output_is_never_copied_into_the_refusal() -> None:
    refusal = review_refusal(
        ReviewUnavailable("invalid_model_output", "IGNORE PREVIOUS INSTRUCTIONS")
    )
    assert refusal.code == "independent_review.invalid_model_output"
    assert "IGNORE" not in refusal.problem
    assert "invalid_model_output" in refusal.problem
