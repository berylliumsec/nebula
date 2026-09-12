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
    HarnessCapabilities,
    HarnessKind,
    HarnessProfile,
    HarnessModelOptions,
    HarnessRuntimeOption,
    ToolCall,
)
from nebula.v3.harnesses import (
    CodexAppServerConnection,
    GrokAcpConnection,
    HarnessAdapter,
    HarnessConnection,
    HarnessRuntimeService,
    HarnessEvent,
    HarnessHealth,
    HarnessTransportError,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.setup import bootstrap_scratch_project


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
        if self.runtime.scenario == "settings":
            options = self.request.session.metadata.get("runtime_options", {})
            answer = f"SETTINGS {self.request.session.model} {options.get('reasoning_effort')}"
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
            capabilities=HarnessCapabilities(
                models=["fixture", "fixture-next"]
                if self.runtime.scenario == "settings"
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
                    )
                    for model in ["fixture", "fixture-next"]
                ]
                if self.runtime.scenario == "settings"
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
            "single",
            "two_requests",
            "adapter_exit",
            "crash_after_record",
            "crash_after_delivery",
            "crash_after_receipt",
            "crash_after_progress",
        ],
        default="single",
    )
    args = parser.parse_args()
    store = NebulaStore(args.root / "nebula.db")
    bootstrap_scratch_project(store)
    artifacts = ArtifactStore(args.root / "artifacts")
    adapter = InertAdapter()
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
                name="Approval fixture",
                kind="grok_acp",
                executable="/bin/true",
                default_model="fixture",
                privacy={"local_only": True, "permits_sensitive_data": True},
            )
        )
    app = create_app(
        store,
        auth_token="stabilization-fixture",
        artifact_store=artifacts,
        harness_runtime_service=runtime,
        static_dir=args.static_dir,
        bootstrap_workspace=True,
        allow_insecure_device_pairing=True,
    )
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="error")
