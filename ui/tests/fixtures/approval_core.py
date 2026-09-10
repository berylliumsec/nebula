"""Disposable real Core with an inert approval-producing adapter.

Only used by acceptance tests. No subprocess commands, model calls or external
targets are executed. HTTP, persistence, UI and approval continuation are real.
"""

import argparse
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
    ToolCall,
)
from nebula.v3.harnesses import (
    HarnessAdapter,
    HarnessConnection,
    HarnessRuntimeService,
    HarnessEvent,
    HarnessHealth,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.setup import bootstrap_scratch_project


class InertConnection(HarnessConnection):
    adapter_version = "stabilization-fixture-v1"
    external_session_id = "inert-session"

    def __init__(self, request, runtime):
        self.request, self.runtime = request, runtime

    async def run_turn(self, prompt, **kwargs):
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
                policy_rationale="Review an inert fixture; no command will execute",
                exact_request={
                    "tool_name": "run_command",
                    "arguments": {"command": "inert fixture, never executed"},
                },
            )
        )
        # This is the same Core waiter used by the command broker, not a mocked
        # HTTP response or manual mutation of the decision/progress transition.
        decision = await self.runtime._wait_for_broker_approval(turn, approval)
        answer = "APPROVAL_ACCEPTED_ONCE" if decision.allowed else "APPROVAL_DECLINED"
        yield HarnessEvent(type="message_delta", delta=answer)
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
            capabilities=HarnessCapabilities(models=["fixture"], interruption=True),
        )

    async def open(self, request):
        return InertConnection(request, self.runtime)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    store = NebulaStore(args.root / "nebula.db")
    bootstrap_scratch_project(store)
    artifacts = ArtifactStore(args.root / "artifacts")
    adapter = InertAdapter()
    runtime = HarnessRuntimeService(
        store,
        credential_store=CredentialStore(),
        workspace_resolver=lambda _: args.root,
        artifact_store=artifacts,
        adapter_factory=lambda _: adapter,
    )
    adapter.runtime = runtime
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
