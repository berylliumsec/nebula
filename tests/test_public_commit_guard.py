"""Exercise the local Git index and Codex verdict boundary with synthetic data."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "public_commit_guard.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def tree(repo: Path) -> str:
    return subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
directory = Path(args[args.index('-C') + 1])
output = Path(args[args.index('-o') + 1])
payload = json.loads(sys.stdin.read().split('STAGED_COMMIT_DATA_JSON:\\n', 1)[1])
files = payload['files']
content = ''.join(item['content'] for item in files)
if payload['commit_message']:
    content += payload['commit_message']
if 'FAIL_REVIEW' in content:
    sys.exit(2)
if 'INDEX_CHANGE' in content:
    repo = Path(os.environ['TEST_REPO'])
    (repo / 'racing.txt').write_text('new staged file')
    subprocess.run(['git', 'add', 'racing.txt'], cwd=repo, check=True)
if 'PRIVATE_RESEARCH' in content:
    decision, category = 'block', 'research_data'
elif 'UNSURE_RESEARCH' in content:
    decision, category = 'uncertain', 'uncertain'
else:
    decision, category = 'allow', 'none'
output.write_text(json.dumps({'decision': decision, 'category': category,
                              'reviewed_files': [] if 'OMIT_FILE' in content else
                              [item['id'] for item in files]}))
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", TEST_REPO=str(tmp_path))
    return tmp_path, env


def run_guard(repo: Path, env: dict[str, str], message: Path | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, str(GUARD)]
    if message:
        command.extend(["--message", str(message)])
    return subprocess.run(command, cwd=repo, env=env, text=True, capture_output=True, check=False)


def test_review_uses_staged_blob_not_unstaged_worktree(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    source = path / "note.txt"
    source.write_text("Public Nebula change\n")
    git(path, "add", "note.txt")
    source.write_text("PRIVATE_RESEARCH only in worktree\n")
    assert run_guard(path, env).returncode == 0


@pytest.mark.parametrize(
    "marker",
    ["PRIVATE_RESEARCH", "UNSURE_RESEARCH", "FAIL_REVIEW", "OMIT_FILE", "INDEX_CHANGE"],
)
def test_sensitive_uncertain_or_failed_review_blocks(
    repo: tuple[Path, dict[str, str]], marker: str
) -> None:
    path, env = repo
    (path / "note.txt").write_text(marker)
    git(path, "add", "note.txt")
    result = run_guard(path, env)
    assert result.returncode == 1
    assert "blocked" in result.stderr


def test_commit_message_is_reviewed(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    message = path / "message.txt"
    message.write_text("PRIVATE_RESEARCH in message\n")
    assert run_guard(path, env, message).returncode == 1


def test_css_is_excluded_by_default_but_commit_message_is_reviewed(
    repo: tuple[Path, dict[str, str]],
) -> None:
    path, env = repo
    (path / "theme.css").write_text("PRIVATE_RESEARCH in an excluded CSS fixture\n")
    git(path, "add", "theme.css")
    assert run_guard(path, env).returncode == 0
    message = path / "message.txt"
    message.write_text("PRIVATE_RESEARCH in commit message\n")
    assert run_guard(path, env, message).returncode == 1


def test_local_config_can_include_css_and_exclude_another_path(
    repo: tuple[Path, dict[str, str]],
) -> None:
    path, env = repo
    config = path / ".git" / "hooks" / "nebula-public-commit-review.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"exclude_globs": ["*.svg"]}))
    (path / "theme.css").write_text("PRIVATE_RESEARCH in included CSS\n")
    (path / "icon.svg").write_text("UNSURE_RESEARCH in excluded SVG\n")
    git(path, "add", "theme.css", "icon.svg")
    assert run_guard(path, env).returncode == 1
    (path / "theme.css").write_text("Public CSS\n")
    git(path, "add", "theme.css")
    assert run_guard(path, env).returncode == 0


def test_local_config_limits_and_unknown_keys_are_enforced(
    repo: tuple[Path, dict[str, str]],
) -> None:
    path, env = repo
    config = path / ".git" / "hooks" / "nebula-public-commit-review.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    (path / "note.txt").write_text("Public text\n")
    git(path, "add", "note.txt")
    config.write_text(json.dumps({"max_total_bytes": 4}))
    assert "review limit" in run_guard(path, env).stderr
    config.write_text(json.dumps({"unknown_setting": True}))
    assert "Unknown local review configuration" in run_guard(path, env).stderr


def test_bypass_requires_exact_tree_reason_and_matching_commit_trailer(
    repo: tuple[Path, dict[str, str]],
) -> None:
    path, env = repo
    (path / "note.txt").write_text("PRIVATE_RESEARCH in bypass fixture\n")
    git(path, "add", "note.txt")
    reason = "Owner approved this exact synthetic test tree"
    approved = {
        **env,
        "NEBULA_PUBLIC_COMMIT_BYPASS_TREE": tree(path),
        "NEBULA_PUBLIC_COMMIT_BYPASS_REASON": reason,
    }
    assert run_guard(path, {**approved, "NEBULA_PUBLIC_COMMIT_BYPASS_TREE": "wrong"}).returncode == 1
    assert run_guard(path, {**approved, "NEBULA_PUBLIC_COMMIT_BYPASS_REASON": ""}).returncode == 1
    assert run_guard(path, approved).returncode == 0
    message = path / "message.txt"
    message.write_text("Synthetic commit\n")
    assert run_guard(path, approved, message).returncode == 1
    message.write_text(f"Synthetic commit\n\nPublic-Review-Bypass: {reason}\n")
    assert run_guard(path, approved, message).returncode == 0
    (path / "another.txt").write_text("new staged content\n")
    git(path, "add", "another.txt")
    assert run_guard(path, approved, message).returncode == 1


def test_binary_staged_content_blocks(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "artifact.bin").write_bytes(b"private\0artifact")
    git(path, "add", "artifact.bin")
    result = run_guard(path, env)
    assert result.returncode == 1
    assert "Binary" in result.stderr


@pytest.mark.parametrize("size, allowed", [(1000 * 1024, True), (1000 * 1024 + 1, False)])
def test_staged_text_file_limit(repo: tuple[Path, dict[str, str]], size: int, allowed: bool) -> None:
    path, env = repo
    (path / "large.txt").write_text("a" * size)
    git(path, "add", "large.txt")
    result = run_guard(path, env)
    assert (result.returncode == 0) is allowed
    if not allowed:
        assert "Staged file exceeds 1024000 bytes" in result.stderr


def test_later_part_of_large_staged_file_can_block(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "large.txt").write_text("Public text.\n" * 10_000 + "PRIVATE_RESEARCH\n")
    git(path, "add", "large.txt")
    result = run_guard(path, env)
    assert result.returncode == 1
    assert "block (research_data)" in result.stderr
    assert "review part 0001." in result.stderr


def test_exact_public_part_does_not_block_new_safe_text(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    source = path / "large.txt"
    public_text = "UNSURE_RESEARCH already published.\n" + "Public text.\n" * 10_000
    source.write_text(public_text)
    git(path, "add", "large.txt")
    git(path, "commit", "-m", "Existing public text")
    source.write_text(public_text + "A new safe line.\n")
    git(path, "add", "large.txt")
    assert run_guard(path, env).returncode == 0


def test_git_commit_invokes_both_hooks(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    scripts = path / "scripts"
    scripts.mkdir()
    shutil.copy2(GUARD, scripts / GUARD.name)
    installer = ROOT / "scripts" / "install_public_commit_guard.py"
    shutil.copy2(installer, scripts / installer.name)
    hooks = path / ".githooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "nebula-public-commit-review.json"):
        shutil.copy2(ROOT / ".githooks" / name, hooks / name)
    installed = subprocess.run(
        [sys.executable, str(scripts / installer.name)],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    (scripts / GUARD.name).unlink()
    source = path / "note.txt"
    source.write_text("Public change\n")
    git(path, "add", "note.txt")
    allowed = subprocess.run(
        ["git", "commit", "-m", "Public note"],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    source.write_text("Another public change\n")
    git(path, "add", "note.txt")
    blocked = subprocess.run(
        ["git", "commit", "-m", "PRIVATE_RESEARCH in commit message"],
        cwd=path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blocked.returncode != 0
    assert "Nebula public commit blocked" in blocked.stderr
    source.write_text("PRIVATE_RESEARCH in a synthetic bypass fixture\n")
    git(path, "add", "note.txt")
    reason = "Owner approved this exact synthetic test tree"
    bypassed = subprocess.run(
        ["git", "commit", "-m", "Synthetic bypass", "-m", f"Public-Review-Bypass: {reason}"],
        cwd=path,
        env={
            **env,
            "NEBULA_PUBLIC_COMMIT_BYPASS_TREE": tree(path),
            "NEBULA_PUBLIC_COMMIT_BYPASS_REASON": reason,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert bypassed.returncode == 0, bypassed.stderr


def test_installer_preserves_local_config_and_refuses_unknown_upgrade(
    repo: tuple[Path, dict[str, str]],
) -> None:
    path, env = repo
    scripts = path / "scripts"
    scripts.mkdir()
    shutil.copy2(GUARD, scripts / GUARD.name)
    installer = ROOT / "scripts" / "install_public_commit_guard.py"
    shutil.copy2(installer, scripts / installer.name)
    hooks = path / ".githooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "nebula-public-commit-review.json"):
        shutil.copy2(ROOT / ".githooks" / name, hooks / name)
    command = [sys.executable, str(scripts / installer.name)]
    assert subprocess.run(command, cwd=path, env=env, capture_output=True, check=False).returncode == 0
    installed = path / ".git" / "hooks"
    config = installed / "nebula-public-commit-review.json"
    assert json.loads(config.read_text())["exclude_globs"] == ["*.css"]
    local_config = '{"exclude_globs": ["*.svg"]}\n'
    config.write_text(local_config)
    assert subprocess.run(command, cwd=path, env=env, capture_output=True, check=False).returncode == 0
    assert config.read_text() == local_config
    guard = installed / "nebula-public-commit-guard.py"
    guard.write_text("unfamiliar hook\n")
    rejected = subprocess.run(command + ["--upgrade"], cwd=path, env=env, capture_output=True, check=False)
    assert rejected.returncode == 1
    assert guard.read_text() == "unfamiliar hook\n"
