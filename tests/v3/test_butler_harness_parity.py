"""Butler entirely on a Claude harness: harness subagents, a harness reviewer,
an existing CLI login, and no native shell.

Fakes only: no network, no SDK process, no vendor account.
"""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nebula.v3 import butler_independent_review as review_module
from nebula.v3 import harnesses as harness_module
from nebula.v3.butler_independent_review import (
    REVIEW_KEY,
    ButlerIndependentReview,
    ReviewUnavailable,
    independent_review_approves,
)
from nebula.v3.butler_sessions import ButlerSessionReservation, reserve_butler_session
from nebula.v3.butler_supervisor_policy import load_policy
from nebula.v3.chat import ChatConfigurationError
from nebula.v3.chat_assistant_config import (
    ChatAssistantConfigPatch,
    chat_assistant_config,
    update_chat_assistant_config,
)
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatBackend,
    HarnessCapabilities,
    Engagement,
    HarnessAuthMode,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    HarnessWorkspaceAccess,
)
from nebula.v3.harness_diagnostics import run_harness_one_shot, run_harness_test_turn
from nebula.v3.harnesses import (
    ClaudeAgentSdkAdapter,
    HarnessConfigurationError,
    HarnessRuntimeService,
    HarnessTurnFailedError,
    _claude_cli_logged_in,
    _scrubbed_claude_environment,
    _session_native_capabilities,
)
from nebula.v3.storage import NebulaStore
from tests.v3.test_butler_claude_harness import (
    CLAUDE_MODEL,
    _Client,
    _claude_harness,
    _fake_sdk,
)
from tests.v3.test_butler_supervisor_powers import (
    _env,
    _FakeStore,
    _FakeWork,
    _goal,
)
from nebula.v3.domain import GOAL_COMPLETION_PROPOSAL_KEY

SHELL_PROFILE = HarnessNativeCapabilities(
    workspace_access=HarnessWorkspaceAccess.READ, shell=True
)


@pytest.fixture(autouse=True)
def _no_kernel_review_globals(monkeypatch):
    # An earlier test that built Core's app leaves the execution kernel's review
    # hooks in these module globals, bound to that test's (closed) store. A
    # review here would fall back to them and reopen that database, leaving an
    # aiosqlite thread that keeps pytest from exiting.
    for name in ("_workspace_resolver", "_harness_runner", "_provider_factory"):
        monkeypatch.setattr(review_module, name, None)


def _runtime(store: NebulaStore, tmp_path: Path) -> HarnessRuntimeService:
    return HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )


# --- 1. Butler subagents on a Claude harness ---------------------------------


async def test_butler_reserves_claude_harness_with_claude_harness_subagents(
    tmp_path,
):
    store = NebulaStore(tmp_path / "harness-children.db")
    project = await store.create(Engagement(name="Selected project"))
    main = await store.create(_claude_harness())
    children = await store.create(_claude_harness(name="Claude children"))

    session = await reserve_butler_session(
        store,
        _runtime(store, tmp_path),
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=main.id,
            model=CLAUDE_MODEL,
            subagent_harness_profile_id=children.id,
            subagent_model="sonnet",
            max_active_subagents=2,
            allow_cloud_tool_results=True,
        ),
    )

    assert session.metadata["provider_subagent"] == {
        "harness_profile_id": children.id,
        "model": "sonnet",
        "max_active": 2,
    }
    assert session.metadata["butler_allow_subagents"] is True
    # The unattended check honors the saved harness child.
    config = await chat_assistant_config(store, session, butler_default=False)
    assert config["allow_subagents"] is True
    assert config["subagent_harness_profile_id"] == children.id
    assert config["subagent_provider_id"] is None
    assert config["subagent_model"] == "sonnet"
    assert config["max_active_subagents"] == 2


async def test_reservation_refuses_ambiguous_or_unusable_harness_children(tmp_path):
    store = NebulaStore(tmp_path / "harness-children-refusals.db")
    project = await store.create(Engagement(name="Selected project"))
    main = await store.create(_claude_harness())
    disabled = await store.create(_claude_harness(enabled=False))
    closed = await store.create(
        _claude_harness(privacy={"permits_sensitive_data": False})
    )

    def request(**child: Any) -> ButlerSessionReservation:
        return ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=main.id,
            model=CLAUDE_MODEL,
            allow_cloud_tool_results=True,
            **child,
        )

    with pytest.raises(ValueError, match="choose one Butler subagent runtime"):
        request(
            subagent_provider_id="provider",
            subagent_harness_profile_id=main.id,
            subagent_model=CLAUDE_MODEL,
        )
    with pytest.raises(ValueError, match="both a runtime and a model"):
        request(subagent_harness_profile_id=main.id)
    with pytest.raises(ValueError, match="Assistant settings"):
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.PROVIDER,
            provider_id="provider",
            subagent_harness_profile_id=main.id,
            subagent_model=CLAUDE_MODEL,
        )
    runtime = _runtime(store, tmp_path)
    for child, model, match in (
        (main.id, "unlisted-model", "advertises"),
        (disabled.id, CLAUDE_MODEL, "advertises"),
        (closed.id, CLAUDE_MODEL, "project data"),
        ("missing", CLAUDE_MODEL, "does not exist"),
    ):
        with pytest.raises(ChatConfigurationError, match=match):
            await reserve_butler_session(
                store,
                runtime,
                request(subagent_harness_profile_id=child, subagent_model=model),
            )


async def test_butler_assistant_settings_accept_a_harness_child(tmp_path):
    store = NebulaStore(tmp_path / "harness-children-settings.db")
    project = await store.create(Engagement(name="Selected project"))
    main = await store.create(_claude_harness())
    session = await reserve_butler_session(
        store,
        _runtime(store, tmp_path),
        ButlerSessionReservation(
            engagement_id=project.id,
            backend=ChatBackend.HARNESS,
            harness_profile_id=main.id,
            model=CLAUDE_MODEL,
            allow_cloud_tool_results=True,
        ),
    )

    saved = await update_chat_assistant_config(
        store,
        session,
        ChatAssistantConfigPatch(
            allow_subagents=True,
            subagent_harness_profile_id=main.id,
            subagent_model=CLAUDE_MODEL,
        ),
        expected_revision=session.revision,
    )

    assert saved.metadata["provider_subagent"]["harness_profile_id"] == main.id


# --- 4. Butler never gets the vendor's native shell ---------------------------


async def test_butler_harness_sessions_never_get_a_native_shell(tmp_path):
    store = NebulaStore(tmp_path / "no-shell.db")
    project = await store.create(Engagement(name="Selected project"))
    profile = await store.create(_claude_harness(native_capabilities=SHELL_PROFILE))
    runtime = _runtime(store, tmp_path)

    async def host_mode(*_args: Any, **_kwargs: Any) -> str:
        return "host"

    # Host execution mode grants every ordinary session a shell.
    runtime._project_execution_mode = host_mode  # type: ignore[method-assign]
    ordinary = await runtime.create_session(
        engagement_id=project.id, profile_id=profile.id, model=CLAUDE_MODEL
    )
    butler = await runtime.create_session(
        engagement_id=project.id,
        profile_id=profile.id,
        model=CLAUDE_MODEL,
        control_assistant=True,
    )

    assert ordinary.metadata["native_capabilities"]["shell"] is True
    assert butler.metadata["native_capabilities"]["shell"] is False
    # A Butler session saved with a shell (older metadata) is clamped when
    # the vendor process is configured.
    legacy = butler.model_copy(
        update={
            "metadata": {
                **butler.metadata,
                "native_capabilities": SHELL_PROFILE.model_dump(mode="json"),
            }
        }
    )
    assert _session_native_capabilities(legacy, profile).shell is False
    assert _session_native_capabilities(ordinary, profile).shell is True


async def test_claude_butler_process_disallows_bash(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    _Client.opened.clear()
    profile = _claude_harness(native_capabilities=SHELL_PROFILE)
    session = HarnessSession(
        engagement_id="project",
        harness_profile_id=profile.id,
        model=CLAUDE_MODEL,
        metadata={
            "control_assistant": True,
            "execution_mode": "host",
            "native_capabilities": SHELL_PROFILE.model_dump(mode="json"),
        },
    )

    async def deny(_request: Any) -> Any:
        raise AssertionError("no permission request expected")

    connection = await ClaudeAgentSdkAdapter().open(
        harness_module.AdapterOpenRequest(
            profile=profile,
            session=session,
            workspace=tmp_path,
            mcp_profiles=(),
            credential_store=CredentialStore(),
            permission_handler=deny,
        )
    )
    await connection.close()

    options = _Client.opened[0].options.kwargs
    assert "Bash" not in options["tools"]
    assert "Bash" in options["disallowed_tools"]
    # Sandbox network restrictions stay on.
    assert options["sandbox"]["network"]["allowedDomains"] == []
    assert options["sandbox"]["allowUnsandboxedCommands"] is False


# --- 2. Independent reviewer on a harness -------------------------------------


class _HarnessReviewStore(_FakeStore):
    def __init__(self, work: _FakeWork, session_model: str) -> None:
        super().__init__(work, session_model)
        self.harness = HarnessProfile(
            id="claude-reviewer",
            name="Claude reviewer",
            kind=HarnessKind.CLAUDE_AGENT_SDK,
            default_model=CLAUDE_MODEL,
            privacy={"permits_sensitive_data": True},
            capabilities=HarnessCapabilities(models=[CLAUDE_MODEL]),
        )

    async def get(self, kind: Any, entity_id: str) -> Any:
        if kind is HarnessProfile:
            assert entity_id == self.harness.id
            return self.harness
        if kind.__name__ != "ChatSession":
            raise AssertionError("a harness review reads no provider profile")
        return self.session


def _harness_review(
    tmp_path: Path,
    verdict: dict[str, Any],
    session_model: str,
    **extra_env: str,
):
    evidence = tmp_path / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "a.md").write_text("decisive fact: the guard runs first\n")
    goal = _goal()
    goal.metadata[GOAL_COMPLETION_PROPOSAL_KEY]["completion_evidence"] = [
        {"path": str(evidence / "a.md")}
    ]
    work = _FakeWork(goal)
    store = _HarnessReviewStore(work, session_model)
    prompts: list[tuple[str, str, str]] = []

    async def runner(profile: HarnessProfile, model: str, prompt: str) -> str:
        prompts.append((profile.id, model, prompt))
        return json.dumps(verdict)

    env = _env(
        tmp_path,
        NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID="claude-reviewer",
        NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS=str(evidence),
        **extra_env,
    )
    service = ButlerIndependentReview(
        store,
        work,
        "p1",
        policy_loader=lambda: load_policy(env),
        provider_factory=lambda _profile: pytest.fail("no provider is used"),
        harness_runner=runner,
    )
    return service, work, prompts


async def test_harness_reviewer_records_provenance_and_approves(tmp_path):
    service, work, prompts = _harness_review(
        tmp_path,
        {"verdict": "ACCEPT", "summary": "holds", "ceiling_justified": True},
        session_model="vendor/goal-model-1",
    )

    result = await service.review("s1", [])

    [(profile_id, model, prompt)] = prompts
    assert (profile_id, model) == ("claude-reviewer", CLAUDE_MODEL)
    assert "decisive fact: the guard runs first" in prompt
    assert "untrusted JSON data" in prompt
    record = work.goals.goal.metadata[REVIEW_KEY]
    assert record["reviewer_harness_profile_id"] == "claude-reviewer"
    assert "reviewer_provider_id" not in record
    assert record["reviewer_tools"] == []
    provenance = record["approval_provenance"]
    assert provenance["harness_profile_id"] == "claude-reviewer"
    assert "provider_id" not in provenance
    assert provenance["model"] == CLAUDE_MODEL
    assert provenance["review_id"] == record["id"]
    assert len(provenance["packet_sha256"]) == 64
    assert independent_review_approves(work.goals.goal, "goal-model-1")[0]
    assert result["applied"]["completed"] is True


async def test_harness_reviewer_refuses_the_goals_own_model_and_two_runtimes(
    tmp_path,
):
    service, _, prompts = _harness_review(
        tmp_path, {"verdict": "ACCEPT", "summary": "ok"}, session_model=CLAUDE_MODEL
    )
    with pytest.raises(ReviewUnavailable) as caught:
        await service.review("s1", [])
    assert caught.value.code == "reviewer_not_independent"

    service, _, prompts = _harness_review(
        tmp_path,
        {"verdict": "ACCEPT", "summary": "ok"},
        session_model="vendor/goal-model-1",
        NEBULA_BUTLER_REVIEW_PROVIDER_ID="anthropic-1",
    )
    with pytest.raises(ReviewUnavailable) as caught:
        await service.review("s1", [])
    assert caught.value.code == "ambiguous_reviewer"
    assert prompts == []


def test_gate_refuses_provenance_naming_both_or_neither_runtime() -> None:
    from tests.v3.test_butler_supervisor_powers import _record

    goal = _goal()
    record = _record(goal)
    record["approval_provenance"]["harness_profile_id"] = "claude-reviewer"
    goal.metadata[REVIEW_KEY] = record
    assert not independent_review_approves(goal, "goal-model-1")[0]
    del record["approval_provenance"]["provider_id"]
    assert independent_review_approves(goal, "goal-model-1")[0]
    del record["approval_provenance"]["harness_profile_id"]
    assert not independent_review_approves(goal, "goal-model-1")[0]


class _OneShotConnection:
    external_session_id = None

    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.closed = False

    async def run_turn(self, _prompt: str, *, model: str) -> Any:
        del model
        for event in self.events:
            yield event

    async def close(self) -> None:
        self.closed = True


def _event(kind: str, **fields: Any) -> Any:
    return SimpleNamespace(
        type=kind,
        message=fields.get("message"),
        delta=fields.get("delta"),
        item_kind=fields.get("item_kind"),
    )


async def test_one_shot_review_turn_is_fresh_and_tool_free():
    profile = _claude_harness(native_capabilities=SHELL_PROFILE)
    requests: list[Any] = []
    connection = _OneShotConnection(
        [_event("message_delta", delta='{"verdict"'), _event("completed", message="{}")]
    )

    async def open_connection(_adapter: Any, request: Any) -> _OneShotConnection:
        requests.append(request)
        return connection

    reply = await run_harness_one_shot(
        profile,
        CLAUDE_MODEL,
        "packet",
        credentials=CredentialStore(),
        adapter_factory=lambda _kind: object(),
        open_connection=open_connection,
    )

    assert reply == "{}"
    [request] = requests
    assert request.profile.native_capabilities == HarnessNativeCapabilities()
    assert request.session.metadata["native_capabilities"] == (
        HarnessNativeCapabilities().model_dump()
    )
    assert request.session.external_session_id is None
    assert request.mcp_profiles == ()
    assert request.gateway_config == {}
    assert request.gateway_tools == ()
    assert "control_assistant" not in request.session.metadata
    assert connection.closed
    with pytest.raises(HarnessConfigurationError, match="cannot use tools"):
        await request.permission_handler(object())


async def test_one_shot_review_turn_fails_on_any_tool_event():
    connection = _OneShotConnection(
        [_event("tool_started", item_kind="tool"), _event("completed", message="{}")]
    )

    async def open_connection(_adapter: Any, _request: Any) -> _OneShotConnection:
        return connection

    with pytest.raises(HarnessConfigurationError, match="attempted a tool"):
        await run_harness_one_shot(
            _claude_harness(),
            CLAUDE_MODEL,
            "packet",
            credentials=CredentialStore(),
            adapter_factory=lambda _kind: object(),
            open_connection=open_connection,
        )
    assert connection.closed


# --- 3. EXISTING_SESSION: the host's Claude CLI login ------------------------


def test_claude_environment_keeps_the_login_location_and_blanks_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/opt/example/claude")
    env = _scrubbed_claude_environment()
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CONFIG_DIR"] == "/opt/example/claude"
    assert env["HOME"] == os.environ["HOME"]
    # An empty value would make the CLI look relative to its cwd.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert "CLAUDE_CONFIG_DIR" not in _scrubbed_claude_environment()


def _fake_cli(tmp_path: Path, body: str) -> Path:
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n" + body)
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


async def test_cli_login_status_reads_auth_status_under_the_turn_environment(
    tmp_path, monkeypatch
):
    seen = tmp_path / "seen.txt"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cli = _fake_cli(
        tmp_path,
        f'echo "$1 $2 $3 key=[$ANTHROPIC_API_KEY]" > {seen}\n'
        'echo \'{"loggedIn": false, "authMethod": "none"}\'\nexit 1\n',
    )
    assert await _claude_cli_logged_in(str(cli)) is False
    assert seen.read_text().strip() == "auth status --json key=[]"

    cli = _fake_cli(tmp_path, "echo '{\"loggedIn\": true}'\n")
    assert await _claude_cli_logged_in(str(cli)) is True
    # An older CLI that cannot answer leaves the login unknown.
    cli = _fake_cli(tmp_path, "echo unknown command >&2\nexit 2\n")
    assert await _claude_cli_logged_in(str(cli)) is None


async def test_check_reports_a_signed_out_existing_session(tmp_path, monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))
    answers: list[bool | None] = [False, True, None]

    async def logged_in(_self: Any, _sdk: Any, _profile: Any) -> bool | None:
        return answers.pop(0)

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_cli_logged_in", logged_in)
    store = NebulaStore(tmp_path / "check.db")
    profile = await store.create(
        _claude_harness(auth_mode=HarnessAuthMode.EXISTING_SESSION)
    )
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: tmp_path,
    )

    signed_out = await runtime.health(profile.id)
    assert signed_out.healthy is False
    assert "not logged in" in (signed_out.detail or "")
    assert "claude auth login" in (signed_out.detail or "")
    assert "HOME" in (signed_out.detail or "")
    saved = await store.get(HarnessProfile, profile.id)
    assert saved.capabilities.authentication_state == "failed"

    signed_in = await runtime.health(profile.id)
    assert signed_in.healthy is True
    assert signed_in.authentication_state == "verified"

    unknown = await runtime.health(profile.id)
    assert unknown.healthy is True
    assert unknown.authentication_state == "unverified"


async def test_check_skips_the_cli_login_for_an_api_key_profile(monkeypatch):
    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_sdk", staticmethod(_fake_sdk))

    async def logged_in(*_args: Any) -> bool:
        raise AssertionError("an API key profile does not read the CLI login")

    monkeypatch.setattr(ClaudeAgentSdkAdapter, "_cli_logged_in", logged_in)
    health = await ClaudeAgentSdkAdapter().probe(
        _claude_harness(auth_mode=HarnessAuthMode.SECRET_REF, secret_ref="env:KEY"),
        CredentialStore(),
    )
    assert health.healthy is True


class _SignedOutConnection:
    external_session_id = None

    async def run_turn(self, _prompt: str, *, model: str) -> Any:
        del model
        raise HarnessTurnFailedError(
            "Claude turn failed: Not logged in · Please run /login",
            reason_code="authentication_failed",
        )
        yield  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


async def test_test_turn_explains_a_signed_out_existing_session(tmp_path):
    store = NebulaStore(tmp_path / "test-turn.db")
    profile = await store.create(
        _claude_harness(auth_mode=HarnessAuthMode.EXISTING_SESSION)
    )

    async def open_connection(_adapter: Any, _request: Any) -> _SignedOutConnection:
        return _SignedOutConnection()

    health = await run_harness_test_turn(
        store,
        profile.id,
        credentials=CredentialStore(),
        adapter_factory=lambda _kind: object(),
        open_connection=open_connection,
    )

    assert health.healthy is False
    assert "not logged in" in (health.detail or "")
    assert "claude auth login" in (health.detail or "")
    saved = await store.get(HarnessProfile, profile.id)
    assert saved.capabilities.authentication_state == "failed"
    assert saved.capabilities.last_turn_failure_reason == "authentication_failed"
    assert saved.capabilities.detail == health.detail


pytestmark = pytest.mark.anyio


async def test_harness_reviewer_is_runtime_agnostic(tmp_path):
    """Any harness kind may review; a Claude goal can be reviewed elsewhere."""

    service, work, prompts = _harness_review(
        tmp_path,
        {"verdict": "ACCEPT", "summary": "holds"},
        session_model=CLAUDE_MODEL,
        NEBULA_BUTLER_REVIEW_MODEL="gpt-test",
    )
    store = service.store
    store.harness = HarnessProfile(
        id="claude-reviewer",
        name="Codex reviewer",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable="/bin/true",
        default_model="gpt-test",
        privacy={"permits_sensitive_data": True},
        capabilities=HarnessCapabilities(models=["gpt-test"]),
    )

    await service.review("s1", [])

    assert prompts[0][1] == "gpt-test"
    record = work.goals.goal.metadata[REVIEW_KEY]
    assert record["approval_provenance"]["model"] == "gpt-test"
    assert independent_review_approves(work.goals.goal, CLAUDE_MODEL)[0]
