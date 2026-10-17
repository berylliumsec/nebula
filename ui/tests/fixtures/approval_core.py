"""Disposable real Core with an inert approval-producing adapter.

Only used by acceptance tests. No subprocess commands, model calls or external
targets are executed. HTTP, persistence, UI and approval continuation are real.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import uvicorn

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    Approval,
    ChatSession,
    ChatTokenUsage,
    HarnessCapabilities,
    HarnessDetailedUsage,
    HarnessKind,
    HarnessProfile,
    HarnessModelOptions,
    HarnessRuntimeOption,
    ProviderProfile,
    OperationEvent,
    ToolCall,
)
from nebula.v3.environments import SshEnvironmentService
from nebula.v3.harnesses import (
    CodexAppServerConnection,
    GrokAcpConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessRuntimeService,
    HarnessEvent,
    HarnessHealth,
    HarnessTransportError,
    HarnessGoalSnapshot,
)
from nebula.v3.ssh_environments import SshProbeResult
from nebula.v3.storage import NebulaStore
from nebula.v3.setup import bootstrap_scratch_project


async def wait_for_committed_progress(
    store: NebulaStore,
    turn_id: str,
    after_sequence: int,
    delta: str,
    *,
    timeout: float = 5.0,
) -> OperationEvent:
    """Read the real ledger barrier; never manufacture progress for the fixture."""
    async with asyncio.timeout(timeout):
        while True:
            events = store.replay_operation_events(
                turn_id,
                after_sequence=after_sequence,
                event_types=["harness.message_delta"],
            )
            for event in events:
                if event.payload.get("delta") == delta:
                    return event
            await asyncio.sleep(0.01)


class InertConnection(HarnessConnection):
    adapter_version = "stabilization-fixture-v1"
    external_session_id = "inert-session"

    def __init__(self, request, runtime):
        self.request, self.runtime = request, runtime

    async def request_decision(self, ordinal):
        turn = self.runtime._active_gateway_turn(self.request.session.id)
        store = self.runtime.store
        call = store.create(
            ToolCall(
                engagement_id=turn.engagement_id,
                run_id=turn.chat_turn_id,
                origin="chat",
                chat_session_id=turn.chat_session_id,
                chat_turn_id=turn.chat_turn_id,
                tool_name="run_command",
                risk_class="passive",
                status="waiting_approval",
                metadata={"harness_turn_id": turn.id},
            )
        )
        approval = store.create(
            Approval(
                engagement_id=turn.engagement_id,
                run_id=turn.chat_turn_id,
                origin="chat",
                chat_session_id=turn.chat_session_id,
                chat_turn_id=turn.chat_turn_id,
                tool_call_id=call.id,
                risk_class="passive",
                requested_by="inert-fixture",
                policy_rationale=f"Review inert fixture {ordinal}; no command will execute",
                exact_request={
                    "tool_name": "run_command",
                    "arguments": {"command": "inert fixture, never executed"},
                },
            )
        )
        # This is the same Core waiter used by the command broker, not a mocked
        # HTTP response or manual mutation of the decision/progress transition.
        decision = await self.runtime._wait_for_broker_approval(turn, approval)
        with (self.runtime.fixture_root / "receipts.jsonl").open("a") as receipt:
            receipt.write(
                json.dumps(
                    {
                        "approval_id": approval.id,
                        "turn_id": turn.id,
                        "allowed": decision.allowed,
                    }
                )
                + "\n"
            )
            receipt.flush()
            os.fsync(receipt.fileno())
        return decision

    async def run_turn(self, prompt, **kwargs):
        if self.request.session.engagement_id == "diagnostic":
            yield HarnessEvent(type="completed", message="OK")
            return
        if self.runtime.scenario == "goal":
            turn = self.runtime._active_gateway_turn(self.request.session.id)
            failure_marker = self.runtime.fixture_root / "goal-failed-once"
            if "FAIL_ONCE" in prompt and not failure_marker.exists():
                failure_marker.write_text(turn.id)
                raise HarnessTransportError("Inert goal fixture failed once")
            block_marker = (
                self.runtime.fixture_root / f"goal-blocked-{turn.chat_session_id}"
            )
            if "BLOCK_ROUTE" in prompt and not block_marker.exists():
                offered = {
                    tool["name"]
                    for tool in self.runtime._gateway_catalog(self.request.session)[
                        "tools"
                    ]
                }
                if "goal.block" not in offered:
                    raise RuntimeError("The active Core goal cannot report a blocker")
                result = await self.runtime._gateway_call(
                    self.request.session,
                    "goal.block",
                    {"reason": "Inert worker route is unavailable"},
                )
                if (
                    result.get("isError")
                    or result.get("structuredContent", {}).get("status") != "blocked"
                ):
                    raise RuntimeError("The Core goal blocker was not saved")
                block_marker.write_text(turn.id)
            yield HarnessEvent(
                type="item_upsert",
                item_id="native-goal",
                item_kind="goal",
                item_status="completed",
                title="Native goal",
                goal=HarnessGoalSnapshot(
                    objective="Native harness review", status="complete"
                ),
            )
            answer = f"GOAL_FIXTURE_REPLY {turn.chat_turn_id}"
            yield HarnessEvent(type="message_delta", delta=answer)
            yield HarnessEvent(
                type="usage",
                usage=ChatTokenUsage(input_tokens=4, output_tokens=5, total_tokens=9),
                detailed_usage=HarnessDetailedUsage(
                    input_tokens=4, output_tokens=5, total_tokens=9
                ),
            )
            yield HarnessEvent(type="completed", message=answer)
            return
        if self.runtime.scenario == "agent_messaging":
            turn = self.runtime._active_gateway_turn(self.request.session.id)
            user_prompt = turn.metadata.get("user_prompt")
            catalog = {
                item["name"]
                for item in self.runtime._gateway_catalog(self.request.session)["tools"]
            }
            if not {"agent.list", "agent.send", "agent.read"} <= catalog:
                raise RuntimeError(
                    "Peer tools were missing from the first enabled turn"
                )
            if user_prompt == "Send a note to peer":
                listed = await self.runtime._gateway_agent_message(
                    self.request.session, turn, "agent.list", {}
                )
                peers = listed["structuredContent"]["agents"]
                if len(peers) != 1:
                    raise RuntimeError(f"Expected one peer, found {len(peers)}")
                await self.runtime._gateway_agent_message(
                    self.request.session,
                    turn,
                    "agent.send",
                    {
                        "session_id": peers[0]["session_id"],
                        "message": "Review the retry boundary.",
                    },
                )
                answer = "Peer note sent"
            elif user_prompt == "Reply to peer":
                await self.runtime._gateway_agent_message(
                    self.request.session, turn, "agent.read", {}
                )
                listed = await self.runtime._gateway_agent_message(
                    self.request.session, turn, "agent.list", {}
                )
                peers = listed["structuredContent"]["agents"]
                if len(peers) != 1:
                    raise RuntimeError(
                        f"Expected one peer for reply, found {len(peers)}"
                    )
                await self.runtime._gateway_agent_message(
                    self.request.session,
                    turn,
                    "agent.send",
                    {
                        "session_id": peers[0]["session_id"],
                        "message": "The retry boundary is idempotent.",
                    },
                )
                answer = "Peer reply sent"
            else:
                answer = "Coordinator ready"
            yield HarnessEvent(type="message_delta", delta=answer)
            yield HarnessEvent(type="completed", message=answer)
            return
        if (
            self.runtime.scenario in {"settings", "daybreak"}
            and "Popup wait for stop" in prompt
        ):
            yield HarnessEvent(
                type="output_delta",
                item_kind="reasoning",
                stream="commentary",
                delta="Checking the selected context before answering.",
            )
            await asyncio.Event().wait()
        if self.runtime.scenario in {"settings", "daybreak"}:
            options = self.request.session.metadata.get("runtime_options", {})
            answer = f"SETTINGS {self.request.session.model} {options.get('reasoning_effort')}"
            if self.runtime.scenario == "daybreak":
                answer += f" {options.get('cyber_access_program')}"
            yield HarnessEvent(
                type="item_upsert",
                vendor=HarnessKind.CODEX_APP_SERVER,
                item_id="fixture-read",
                item_kind="tool",
                item_status="failed",
                title="Workspace read",
                summary="Fixture workspace argument error",
                payload={"detail": "Fixture workspace argument error"},
            )
            yield HarnessEvent(type="message_delta", delta=answer)
            yield HarnessEvent(type="completed", message=answer)
            return
        decisions = await asyncio.gather(
            *(
                self.request_decision(index + 1)
                for index in range(2 if self.runtime.scenario == "two_requests" else 1)
            )
        )
        if self.runtime.scenario == "adapter_exit":
            raise HarnessTransportError(
                "Inert adapter exited after its decision receipt"
            )
        if self.runtime.scenario == "crash_after_receipt":
            os._exit(83)
        answer = (
            "APPROVAL_ACCEPTED_ONCE"
            if all(decision.allowed for decision in decisions)
            else "APPROVAL_DECLINED"
        )
        yield HarnessEvent(type="message_delta", delta=answer)
        if self.runtime.scenario == "crash_after_progress":
            # The runtime reads ahead while coalescing deltas. A yielded fragment
            # is not committed progress yet; crash only after the named boundary.
            turn = self.runtime._active_gateway_turn(self.request.session.id)
            continuations = [
                approval.continuation
                for approval in self.runtime.store.list_entities(Approval)
                if approval.continuation
                and approval.continuation.harness_turn_id == turn.id
                and approval.continuation.status == "delivered"
            ]
            if not continuations or any(
                continuation.progress_after_sequence is None
                for continuation in continuations
            ):
                raise RuntimeError("Inert fixture has no delivered progress boundary")
            after_sequence = max(
                continuation.progress_after_sequence for continuation in continuations
            )
            progress = await wait_for_committed_progress(
                self.runtime.store, turn.id, after_sequence, answer
            )
            checkpoint = {
                "harness_turn_id": turn.id,
                "after_sequence": after_sequence,
                "progress_sequence": progress.sequence,
                "event_type": progress.event_type,
                "delta": progress.payload["delta"],
                "completed_events": len(
                    self.runtime.store.replay_operation_events(
                        turn.id, event_types=["harness.completed"]
                    )
                ),
            }
            with (self.runtime.fixture_root / "progress-checkpoint.json").open(
                "w"
            ) as proof:
                json.dump(checkpoint, proof)
                proof.flush()
                os.fsync(proof.fileno())
            os._exit(84)
        yield HarnessEvent(type="completed", message=answer)

    async def interrupt(self):
        pass

    async def steer(self, text):
        raise RuntimeError("Fixture steering is unsupported")

    async def close(self):
        pass


class InertAdapter(HarnessAdapter):
    kind = HarnessKind.GROK_ACP

    async def probe(self, profile, credential_store):
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=self.kind,
            authentication_state="verified",
            capabilities=HarnessCapabilities(
                detailed_usage=self.runtime.scenario == "goal",
                models=["fixture", "fixture-next"]
                if self.runtime.scenario in {"settings", "daybreak", "goal"}
                else ["fixture"],
                interruption=True,
                model_options=[
                    HarnessModelOptions(
                        model=model,
                        reasoning_efforts=[
                            HarnessRuntimeOption(id="low", label="Low"),
                            HarnessRuntimeOption(id="high", label="High"),
                        ],
                        default_reasoning_effort="low",
                        cyber_access_programs=(
                            ["standard", "daybreakBlue"]
                            if self.runtime.scenario == "daybreak"
                            else None
                        ),
                    )
                    for model in ["fixture", "fixture-next"]
                ]
                if self.runtime.scenario in {"settings", "daybreak", "goal"}
                else [],
            ),
        )

    async def open(self, request):
        if self.runtime.scenario == "commands":
            from command_peer import CommandPeer

            cls = (
                CodexAppServerConnection
                if request.profile.kind == HarnessKind.CODEX_APP_SERVER
                else GrokAcpConnection
            )
            return cls(
                CommandPeer(self.runtime.fixture_root, request.session.id),
                external_session_id=request.session.id,
                permission_handler=None,
            )
        return InertConnection(request, self.runtime)


class FailureInjectionRuntime(HarnessRuntimeService):
    """Process barriers belong only to this inert acceptance executable."""

    async def resolve_approval(self, approval):
        if self.scenario == "crash_after_record":
            os._exit(81)
        await super().resolve_approval(approval)

    def _deliver_approval(self, approval, related_turn, future):
        super()._deliver_approval(approval, related_turn, future)
        if self.scenario == "crash_after_delivery":
            os._exit(82)


ENVIRONMENTS_SSH_CONFIG = """
Host *
    ServerAliveInterval 30

Host lab-mac lab-mac.local
    # Nebula-Purpose: Build and test machine for the lab.
    # Nebula-Avoid: Production deployment.
    HostName 127.0.0.1
    User research

Host pi-one
    HostName 127.0.0.2
    User kali
"""


def environments_fixture(store: NebulaStore, root: Path) -> SshEnvironmentService:
    """Real settings storage and ssh -G over a fixture config; connection tests are scripted."""

    config = root / "ssh_config"
    config.write_text(ENVIRONMENTS_SSH_CONFIG)

    def probe(alias: str) -> SshProbeResult:
        if alias == "pi-one":
            return SshProbeResult(
                alias=alias,
                status="host_key_untrusted",
                latency_ms=None,
                detail="Host key verification failed.",
            )
        return SshProbeResult(
            alias=alias,
            status="reachable",
            latency_ms=12,
            system="Darwin",
            os_version="macOS 27.0",
            arch="arm64",
            model="Mac17,5",
            tools=("python3", "git", "xcrun"),
            passwordless_sudo=False,
        )

    return SshEnvironmentService(store, config_path=config, probe=probe)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--scenario",
        choices=[
            "commands",
            "settings",
            "daybreak",
            "single",
            "two_requests",
            "adapter_exit",
            "crash_after_record",
            "crash_after_delivery",
            "crash_after_receipt",
            "crash_after_progress",
            "environments",
            "sidebar",
            "agent_messaging",
            "goal",
            "navigation",
        ],
        default="single",
    )
    args = parser.parse_args()
    store = NebulaStore(args.root / "nebula.db")
    scratch_id = bootstrap_scratch_project(store)
    if args.scenario == "sidebar" and scratch_id:
        if not any(
            profile.id == "sidebar-provider"
            for profile in store.list_entities(ProviderProfile)
        ):
            store.create(
                ProviderProfile(
                    id="sidebar-provider",
                    name="Sidebar fixture",
                    provider_type="vllm",
                    is_local=True,
                    model_allowlist=["fixture"],
                )
            )
        if not any(
            session.id == "sidebar-parent"
            for session in store.list_entities(ChatSession)
        ):
            for session_id, title, parent_id, metadata in (
                ("sidebar-parent", "Main investigation", None, {}),
                (
                    "sidebar-child",
                    "Subagent · API mapping",
                    "sidebar-parent",
                    {"subagent_id": "fixture-subagent"},
                ),
                ("sidebar-branch", "Ordinary branch", "sidebar-parent", {}),
            ):
                store.create(
                    ChatSession(
                        id=session_id,
                        engagement_id=scratch_id,
                        title=title,
                        provider_profile_id="sidebar-provider",
                        parent_session_id=parent_id,
                        model="fixture",
                        metadata=metadata,
                    )
                )
    if args.scenario == "navigation" and scratch_id:
        store.create(
            ProviderProfile(
                id="navigation-provider",
                name="Navigation fixture",
                provider_type="vllm",
                is_local=True,
                model_allowlist=["fixture"],
                enabled=False,
            )
        )
        for index in range(101):
            store.create(
                ChatSession(
                    id="navigation-older" if index == 0 else f"navigation-recent-{index}",
                    engagement_id=scratch_id,
                    title="Older pinned context" if index == 0 else f"Recent context {index}",
                    provider_profile_id="navigation-provider",
                    model="fixture",
                )
            )
    artifacts = ArtifactStore(args.root / "artifacts")
    adapter = InertAdapter()
    if args.scenario == "daybreak":
        adapter.kind = HarnessKind.CODEX_APP_SERVER
    runtime = FailureInjectionRuntime(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: args.root,
        artifact_store=artifacts,
        adapter_factory=lambda _: adapter,
    )
    runtime.fixture_root = args.root
    runtime.scenario = args.scenario
    adapter.runtime = runtime
    if not any(
        profile.id == "inert-fixture" for profile in store.list_entities(HarnessProfile)
    ):
        store.create(
            HarnessProfile(
                id="inert-fixture",
                name="Codex Daybreak fixture"
                if args.scenario == "daybreak"
                else "Approval fixture",
                kind="codex_app_server" if args.scenario == "daybreak" else "grok_acp",
                executable="/bin/true",
                default_model="fixture",
                privacy={"local_only": True, "permits_sensitive_data": True},
                capabilities=HarnessCapabilities(
                    detailed_usage=args.scenario == "goal"
                ),
            )
        )
    ssh_service = None
    if args.scenario == "environments":
        ssh_service = environments_fixture(store, args.root)
    app = create_app(
        store,
        auth_token="stabilization-fixture",
        ssh_environment_service=ssh_service,
        artifact_store=artifacts,
        harness_runtime_service=runtime,
        static_dir=args.static_dir,
        bootstrap_workspace=True,
        allow_insecure_device_pairing=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="error")
