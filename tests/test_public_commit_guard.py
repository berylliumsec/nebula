"""Exercise the local Git index and reviewer verdict boundary with synthetic data."""

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


FAKE_REVIEWER = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

KIND = {kind!r}
args = sys.argv[1:]
repo = Path(os.environ['TEST_REPO'])
prompt = sys.stdin.read()
with (repo / '.git' / 'reviewer-calls.jsonl').open('a') as calls:
    calls.write(json.dumps({{'kind': KIND, 'args': args, 'prompt': prompt,
                            'simple': os.environ.get('CLAUDE_CODE_SIMPLE')}}) + '\\n')
if os.environ.get('FAKE_' + KIND.upper()) == 'down':
    sys.exit(3)
payload = json.loads(prompt.split('STAGED_COMMIT_DATA_JSON:\\n', 1)[1])
files = payload['files']
content = ''.join(item['content'] for item in files)
if payload['commit_message']:
    content += payload['commit_message']
if 'FAIL_REVIEW' in content:
    sys.exit(2)
if 'INDEX_CHANGE' in content:
    (repo / 'racing.txt').write_text('new staged file')
    subprocess.run(['git', 'add', 'racing.txt'], cwd=repo, check=True)
if 'PRIVATE_RESEARCH' in content:
    decision, category = 'block', 'research_data'
elif 'UNSURE_RESEARCH' in content:
    decision, category = 'uncertain', 'uncertain'
else:
    decision, category = 'allow', 'none'
verdict = {{'decision': decision, 'category': category,
           'reviewed_files': [] if 'OMIT_FILE' in content else [item['id'] for item in files]}}
if os.environ.get('FAKE_' + KIND.upper()) == 'malformed':
    verdict = {{'decision': 'allow'}}
if KIND == 'codex':
    Path(args[args.index('-o') + 1]).write_text(json.dumps(verdict))
else:
    print(json.dumps({{'type': 'result', 'is_error': False, 'structured_output': verdict}}))
"""


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for kind in ("codex", "claude"):
        reviewer = bin_dir / kind
        reviewer.write_text(FAKE_REVIEWER.format(kind=kind), encoding="utf-8")
        reviewer.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", TEST_REPO=str(tmp_path))
    for key in ("FAKE_CODEX", "FAKE_CLAUDE"):
        env.pop(key, None)
    return tmp_path, env


def reviewer_calls(repo: Path) -> list[dict]:
    log = repo / ".git" / "reviewer-calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def receipts(repo: Path) -> list[dict]:
    log = repo / ".git" / "nebula-public-commit-reviews.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def write_config(repo: Path, config: dict) -> Path:
    path = repo / ".git" / "hooks" / "nebula-public-commit-review.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


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


def test_codex_reviews_first_and_claude_is_not_called(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, env)
    assert result.returncode == 0, result.stderr
    assert "review passed (codex)" in result.stderr
    assert [call["kind"] for call in reviewer_calls(path)] == ["codex"]
    assert receipts(path)[-1]["reviewers"] == ["codex"]
    assert receipts(path)[-1]["tree"] == tree(path)


@pytest.mark.parametrize("mode", ["down", "malformed"])
def test_unavailable_codex_falls_back_to_claude(repo: tuple[Path, dict[str, str]], mode: str) -> None:
    path, env = repo
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, {**env, "FAKE_CODEX": mode, "CLAUDE_CODE_SIMPLE": "1"})
    assert result.returncode == 0, result.stderr
    assert "review passed (claude)" in result.stderr
    calls = reviewer_calls(path)
    assert [call["kind"] for call in calls] == ["codex", "claude"]
    claude = calls[1]
    assert claude["simple"] is None  # the parent session's bare-mode flag is not inherited
    assert claude["args"][claude["args"].index("--tools") + 1] == ""
    assert "--strict-mcp-config" in claude["args"]
    assert claude["args"][claude["args"].index("--setting-sources") + 1] == ""


def test_fallback_never_overrides_a_block(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "note.txt").write_text("PRIVATE_RESEARCH\n")
    git(path, "add", "note.txt")
    result = run_guard(path, env)
    assert result.returncode == 1
    assert "codex marked the commit block (research_data)" in result.stderr
    assert [call["kind"] for call in reviewer_calls(path)] == ["codex"]
    assert receipts(path) == []


def test_unavailable_reviewer_is_skipped_for_later_parts(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "large.txt").write_text("Public text.\n" * 1_000)
    git(path, "add", "large.txt")
    result = run_guard(path, {**env, "FAKE_CODEX": "down"})
    assert result.returncode == 0, result.stderr
    kinds = [call["kind"] for call in reviewer_calls(path)]
    assert kinds[0] == "codex" and kinds.count("codex") == 1
    assert kinds.count("claude") > 1


def test_every_reviewer_unavailable_blocks(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, {**env, "FAKE_CODEX": "down", "FAKE_CLAUDE": "down"})
    assert result.returncode == 1
    assert "No configured reviewer was available: codex review failed" in result.stderr
    assert "claude review failed" in result.stderr


def test_local_config_orders_reviewers_and_sets_models(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    write_config(path, {"reviewers": [{"kind": "claude", "model": "opus"}, {"kind": "codex", "model": "gpt-5.5"}]})
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, {**env, "FAKE_CLAUDE": "down"})
    assert result.returncode == 0, result.stderr
    assert "review passed (codex (gpt-5.5))" in result.stderr
    claude, codex = reviewer_calls(path)
    assert claude["args"][claude["args"].index("--model") + 1] == "opus"
    assert codex["args"][codex["args"].index("-m") + 1] == "gpt-5.5"


@pytest.mark.parametrize(
    "reviewers",
    [[], [{"kind": "shell"}], [{"kind": "codex", "model": "--yolo"}], [{"kind": "codex", "argv": ["x"]}],
     [{"kind": "codex", "timeout_seconds": 1}]],
)
def test_invalid_reviewer_config_blocks(repo: tuple[Path, dict[str, str]], reviewers: list) -> None:
    path, env = repo
    write_config(path, {"reviewers": reviewers})
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, env)
    assert result.returncode == 1
    assert reviewer_calls(path) == []


def test_private_policy_file_is_sent_to_the_reviewer(repo: tuple[Path, dict[str, str]]) -> None:
    path, env = repo
    hooks = write_config(path, {"policy_file": "nebula-public-commit-policy.txt"}).parent
    (hooks / "nebula-public-commit-policy.txt").write_text("Block anything naming the synthetic Zebra project.\n")
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    assert run_guard(path, env).returncode == 0
    prompt = reviewer_calls(path)[0]["prompt"]
    assert "LOCAL_POLICY:\nBlock anything naming the synthetic Zebra project." in prompt
    assert prompt.index("LOCAL_POLICY") < prompt.index("\nSTAGED_COMMIT_DATA_JSON:\n")
    (hooks / "nebula-public-commit-policy.txt").unlink()
    missing = run_guard(path, env)
    assert missing.returncode == 1
    assert "policy file is missing" in missing.stderr


def test_public_prompt_names_no_private_program() -> None:
    text = GUARD.read_text(encoding="utf-8").lower()
    assert "bug bounty" not in text


COMMAND_REVIEWER = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

request = json.loads(sys.stdin.read())
repo = Path(os.environ['TEST_REPO'])
with (repo / '.git' / 'reviewer-calls.jsonl').open('a') as calls:
    calls.write(json.dumps({'kind': 'command', 'args': sys.argv[1:], 'prompt': request['prompt'],
                            'schema': request['schema'], 'simple': os.environ.get('CLAUDE_CODE_SIMPLE')}) + '\\n')
mode = os.environ.get('FAKE_COMMAND')
if mode == 'down':
    print('Core is not reachable at the local origin', file=sys.stderr)
    sys.exit(4)
payload = json.loads(request['prompt'].split('STAGED_COMMIT_DATA_JSON:\\n', 1)[1])
files = payload['files']
content = ''.join(item['content'] for item in files)
if 'PRIVATE_RESEARCH' in content:
    verdict = {'decision': 'block', 'category': 'research_data', 'reviewed_files': [item['id'] for item in files]}
else:
    verdict = {'decision': 'allow', 'category': 'none', 'reviewed_files': [item['id'] for item in files]}
print('not json' if mode == 'garbage' else json.dumps(verdict))
"""


@pytest.fixture
def command_reviewer(repo: tuple[Path, dict[str, str]]) -> Path:
    path, _ = repo
    program = path / "bin" / "nebula-public-review"
    program.write_text(COMMAND_REVIEWER, encoding="utf-8")
    program.chmod(0o755)
    return program


def test_command_reviewer_gets_prompt_and_schema_and_decides(
    repo: tuple[Path, dict[str, str]], command_reviewer: Path
) -> None:
    path, env = repo
    write_config(path, {"reviewers": [{"kind": "command", "argv": [str(command_reviewer), "--origin", "x"],
                                       "label": "Nebula Butler"}]})
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, {**env, "CLAUDE_CODE_SIMPLE": "1"})
    assert result.returncode == 0, result.stderr
    assert "review passed (Nebula Butler)" in result.stderr
    (call,) = reviewer_calls(path)
    assert call["args"] == ["--origin", "x"]
    assert call["schema"]["required"] == ["decision", "category", "reviewed_files"]
    assert call["simple"] is None
    assert receipts(path)[-1]["reviewers"] == ["Nebula Butler"]
    (path / "note.txt").write_text("PRIVATE_RESEARCH\n")
    git(path, "add", "note.txt")
    blocked = run_guard(path, env)
    assert blocked.returncode == 1
    assert "Nebula Butler marked the commit block (research_data)" in blocked.stderr


@pytest.mark.parametrize("mode", ["down", "garbage"])
def test_unavailable_command_reviewer_fails_closed_with_its_reason(
    repo: tuple[Path, dict[str, str]], command_reviewer: Path, mode: str
) -> None:
    path, env = repo
    write_config(path, {"reviewers": [{"kind": "command", "argv": [str(command_reviewer)]}]})
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    result = run_guard(path, {**env, "FAKE_COMMAND": mode})
    assert result.returncode == 1
    assert "No configured reviewer was available: command (nebula-public-review)" in result.stderr
    if mode == "down":
        assert "Core is not reachable at the local origin" in result.stderr
    assert [call["kind"] for call in reviewer_calls(path)] == ["command"]


@pytest.mark.parametrize(
    "reviewer",
    [{"kind": "command"}, {"kind": "command", "argv": ["relative/review"]},
     {"kind": "command", "argv": ["/bin/true"], "model": "opus"}, {"kind": "codex", "argv": ["/bin/true"]},
     {"kind": "command", "argv": ["/bin/true"], "label": "bad\nlabel"}],
)
def test_invalid_command_reviewer_config_blocks(repo: tuple[Path, dict[str, str]], reviewer: dict) -> None:
    path, env = repo
    write_config(path, {"reviewers": [reviewer]})
    (path / "note.txt").write_text("Public change\n")
    git(path, "add", "note.txt")
    assert run_guard(path, env).returncode == 1
    assert reviewer_calls(path) == []
