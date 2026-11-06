"""Inert saved inspection data and a cancellable asyncio waiter; no code runs."""

import asyncio
import hashlib
from typing import Any
from uuid import uuid4

from nebula.v3 import cli
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    Engagement,
    ExecutionOrigin,
    ExecutionRuntimeSnapshot,
    KnowledgeSource,
    OperatorExecution,
    ProviderProfile,
    RiskClass,
    ToolCall,
    utc_now,
)


async def seed_inspection(store, artifacts):
    project = await store.create(Engagement(name="Inspection fixture"))
    foreign = await store.create(Engagement(name="Other inspection fixture"))
    provider = await store.create(
        ProviderProfile(
            name="Inspection model",
            provider_type="vllm",
            enabled=False,
            is_local=True,
            model_allowlist=["security-model"],
            metadata={
                "default_model": "security-model",
                "options": {"context_window": 32768},
            },
        )
    )
    chat = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Inspection conversation",
            provider_profile_id=provider.id,
            model="security-model",
        )
    )
    turn = await store.create(
        ChatTurn(
            engagement_id=project.id,
            session_id=chat.id,
            provider_profile_id=provider.id,
            model="security-model",
            status="complete",
            reasoning="Synthetic saved reasoning. " * 500,
        )
    )
    message = await store.create(
        ChatMessage(
            engagement_id=project.id,
            session_id=chat.id,
            sequence=1,
            role="assistant",
            content="Synthetic saved answer.\n```text\nA saved inert excerpt\n```",
            metadata={"chat_turn_id": turn.id},
        )
    )
    call = await store.create(
        ToolCall(
            engagement_id=project.id,
            run_id=turn.id,
            origin="chat",
            chat_session_id=chat.id,
            chat_turn_id=turn.id,
            tool_name="fixture.observe",
            status="complete",
            risk_class=RiskClass.LOCAL_READ,
        )
    )
    raw_bytes = (
        b"progress recorded\npassword=RAW_INSPECTION_SECRET\nfinished observation\n"
    )
    output = await store.create(
        artifacts.put_bytes(
            raw_bytes,
            engagement_id=project.id,
            filename="observation.txt",
            media_type="text/plain",
            metadata={"tool_call_id": call.id, "kind": "stdout", "searchable": True},
        )
    )
    source_artifact = await store.create(
        artifacts.put_bytes(
            b"SAVED_SOURCE_SELECTION_PRIVATE\n",
            engagement_id=project.id,
            filename="selected-source.txt",
            media_type="text/plain",
        )
    )
    source = await store.create(
        KnowledgeSource(
            engagement_id=project.id,
            name="selected-source.txt",
            source_type="text/plain",
            artifact_id=source_artifact.id,
            status="ready",
            document_count=1,
        )
    )

    async def execution(name, status):
        identity = str(uuid4())
        source_bytes = f"# Inert {name} source, never executed.\n".encode()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source = await store.create(
            artifacts.put_bytes(
                source_bytes,
                engagement_id=project.id,
                filename=f"{name}.py",
                media_type="text/plain",
                metadata={"execution_id": identity},
            )
        )
        raw = await store.create(
            artifacts.put_bytes(
                raw_bytes,
                engagement_id=project.id,
                filename=f"{name}-original.txt",
                media_type="text/plain",
                metadata={"execution_id": identity, "stream": "stdout"},
            )
        )
        redacted = artifacts.put_bytes(
            b"progress recorded\npassword=[REDACTED]\n" * 200,
            engagement_id=project.id,
            filename=f"{name}-redacted.txt",
            media_type="text/plain",
            parent_artifact_id=raw.id,
            metadata={"execution_id": identity, "redacted": True},
        )
        redacted = await store.create(redacted.model_copy(update={"redacted": True}))
        return await store.create(
            OperatorExecution(
                id=identity,
                engagement_id=project.id,
                operator_id="synthetic-operator",
                origin=ExecutionOrigin(
                    kind="selection",
                    source_kind="fixture",
                    source_id=name,
                    source_label=name,
                    source_sha256=source_hash,
                ),
                language="python",
                source_sha256=source_hash,
                source_artifact_id=source.id,
                source_preview=source_bytes.decode(),
                runtime=ExecutionRuntimeSnapshot(
                    language="python",
                    interpreter="python",
                    runtime_digest="sha256:" + "a" * 64,
                    image="registry.invalid/inert@sha256:" + "a" * 64,
                    runner_profile_id="inert",
                    runner_profile_revision=1,
                    runner_runtime="podman",
                    runner_isolation="rootless",
                    runner_executable="/inert/not-a-program",
                    runner_platform="linux/amd64",
                ),
                preview_fingerprint="a" * 64,
                request_fingerprint="b" * 64,
                client_idempotency_key=name,
                status=status,
                stdout_artifact_id=raw.id,
                redacted_stdout_artifact_id=redacted.id,
                completed_at=utc_now() if status == "completed" else None,
            )
        )

    completed = await execution("Completed inspection", "completed")
    waiting = await execution("Waiting inspection", "running")
    return {
        "project": project,
        "foreign": foreign,
        "provider": provider,
        "chat": chat,
        "turn": turn,
        "message": message,
        "call": call,
        "output": output,
        "source": source,
        "raw_bytes": raw_bytes,
        "completed": completed,
        "waiting": waiting,
    }


async def attach_inert_waiter(store, service, execution):
    started = asyncio.Event()

    async def wait():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            current = await store.get(OperatorExecution, execution.id)
            (
                await store.update(
                    OperatorExecution,
                    execution.id,
                    {"status": "cancelled", "completed_at": utc_now()},
                    expected_revision=current.revision,
                )
            )

    task = asyncio.create_task(wait())
    service._tasks[execution.id] = task
    await started.wait()
    return task


_create_app = cli.create_app


def create_fixture_app(*args: Any, **kwargs: Any):
    app = _create_app(*args, **kwargs)
    store = app.state.store
    artifacts = app.state.artifact_store
    service = app.state.execution_service
    if service is None or artifacts is None:
        raise RuntimeError(
            "Inspection fixture requires Core's configured execution service and artifact store"
        )
    startup = service.startup

    async def initialize():
        await startup()
        data = await seed_inspection(store, artifacts)
        await attach_inert_waiter(store, service, data["waiting"])

    service.startup = initialize
    return app


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
