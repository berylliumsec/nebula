import hashlib
import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts.playwright_impact import main, select_plan
from scripts.test_selection import change_digest, validate

ROOT = Path(__file__).parents[1]
MANIFEST = json.loads((ROOT / "ui/playwright-impact.json").read_text())


def fixture_plan():
    return dict(
        change_digest="current",
        reason="Selection policy only",
        reviewed_by="agent",
        exclusions="No application or native behavior changes",
        python=["tests/test_test_selection.py"],
        frontend=[],
        native=[],
        playwright=[],
        expected_tests=dict(python=1, frontend=0, native=0, playwright=0),
    )


def test_reviewed_plan_and_deliberate_empty_layers():
    assert validate(fixture_plan(), "current")["playwright"] == []


@pytest.mark.parametrize("field", ["reason", "reviewed_by", "exclusions"])
def test_review_cannot_be_omitted(field):
    plan = fixture_plan()
    plan[field] = " "
    with pytest.raises(ValueError):
        validate(plan, "current")


def test_stale_plan_blocks():
    with pytest.raises(ValueError, match="stale"):
        validate(fixture_plan(), "new-diff")


@pytest.mark.parametrize(
    "target",
    [
        "tests",
        "tests/v3",
        "-q",
        "tests/**/*.py",
        "tests/../pyproject.toml",
        "tests/not-present.py",
    ],
)
def test_broad_or_invalid_targets_block(target):
    plan = fixture_plan()
    plan["python"] = [target]
    with pytest.raises(ValueError):
        validate(plan, "current")


def test_all_project_union_cannot_bypass_full_approval():
    result = select_plan(
        MANIFEST,
        "base",
        "head",
        [],
        selection=[f"project:{e['project']}" for e in MANIFEST["full_include"]],
        review_reason="Attempted full coverage via selectors",
    )
    assert result["coverage_review_required"]
    assert result["include"] == []


def test_empty_override_requires_reason():
    with pytest.raises(ValueError):
        select_plan(MANIFEST, None, "head", [], selection=["none"])
    assert (
        select_plan(
            MANIFEST,
            None,
            "head",
            [],
            selection=["none"],
            review_reason="Documentation only",
        )["include"]
        == []
    )


def test_blocked_cli_writes_receipt_and_exits_nonzero(tmp_path):
    output, receipt = tmp_path / "plan.json", tmp_path / "receipt.md"
    assert (
        main(
            [
                "select",
                "--manifest",
                str(ROOT / "ui/playwright-impact.json"),
                "--candidate",
                "HEAD",
                "--output",
                str(output),
                "--receipt",
                str(receipt),
            ]
        )
        == 2
    )
    assert json.loads(output.read_text())["include"] == []
    assert "coverage_review_required" in receipt.read_text()


def test_digest_binds_content_not_commit_id_and_excludes_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )
    (tmp_path / "file").write_text("one")
    subprocess.run(["git", "add", "file"], check=True)
    first = change_digest("HEAD", "WORKTREE")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/test-selection.json").write_text("receipt")
    subprocess.run(["git", "add", ".github"], check=True)
    assert change_digest("HEAD", "WORKTREE") == first
    (tmp_path / "file").write_text("two")
    assert change_digest("HEAD", "WORKTREE") != first
    assert first != hashlib.sha256(b"").hexdigest()


def test_local_guard_without_starting_any_test_runtime():
    source = """
      import {assertFocused} from './scripts/test_scope_guard.mjs';
      import assert from 'node:assert/strict';
      assert.throws(() => assertFocused('e2e', [], {}));
      assert.throws(() => assertFocused('unit', [], {}));
      assert.throws(() => assertFocused('e2e', ['tests/interface.spec.ts'], {}));
      assert.throws(() => assertFocused('e2e', ['tests/interface.spec.ts', '--project=*'], {}));
      assert.throws(() => assertFocused('unit', [], {NEBULA_FULL_SUITE_APPROVAL:'RUN_FULL_SUITE'}));
      assertFocused('unit', ['src/api/runtimeDefaults.test.ts'], {});
      assertFocused('e2e', ['tests/interface.spec.ts','--project=desktop'], {});
      assertFocused('e2e', ['--list'], {});
      assertFocused('e2e', [], {NEBULA_FULL_SUITE_APPROVAL:'RUN_FULL_SUITE', NEBULA_FULL_SUITE_REASON:'explicit user approval'});
    """
    subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT, check=True)


def test_pytest_guard_requires_files_or_explicit_approval(monkeypatch):
    from conftest import pytest_configure

    monkeypatch.delenv("NEBULA_FULL_SUITE_APPROVAL", raising=False)
    config = SimpleNamespace(
        option=SimpleNamespace(collectonly=False), args=["tests/v3"]
    )
    with pytest.raises(pytest.UsageError):
        pytest_configure(config)
    config.option.collectonly = True
    pytest_configure(config)
    config.option.collectonly = False
    config.args = ["tests/test_test_selection.py"]
    pytest_configure(config)
    config.args = ["tests/v3"]
    monkeypatch.setenv("NEBULA_FULL_SUITE_APPROVAL", "RUN_FULL_SUITE")
    monkeypatch.setenv("NEBULA_FULL_SUITE_REASON", "User approved this run")
    pytest_configure(config)


def test_workflows_never_implicitly_expand_test_targets():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    matrix = (ROOT / ".github/workflows/playwright-impact.yml").read_text()
    assert "pytest -q tests/v3\n" not in ci
    assert "npm test -- --testTimeout" not in ci
    assert "scripts/test_selection.py" in ci and "scripts/test_selection.py" in matrix
    assert 'test "$EVENT_NAME" = workflow_dispatch' in matrix
    assert 'test "$FULL_APPROVAL" = RUN_FULL_SUITE' in matrix
    assert "args+=(--full-approved)" in matrix
