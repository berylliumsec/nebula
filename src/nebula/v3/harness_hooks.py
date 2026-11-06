"""Core-owned, immutable lifecycle guards around vendor-managed chat turns."""

from __future__ import annotations

from .async_utils import resolve_workspace

from typing import Any

from .domain import (
    ChatTurn,
    HarnessTurn,
    HarnessTurnStatus,
    NativeHookExecution,
    utc_now,
)
from .storage import ConflictError, NotFoundError
from .native_hooks import NativeHookRunner, NativeHookSnapshot
from .workspace_provenance import WorkspaceProvenanceService, actor_id_for


class HarnessHookBlocked(RuntimeError):
    pass


async def run_turn_hooks(
    runtime: Any, turn: Any, event_name: str, payload: dict[str, Any] | None = None
) -> None:
    if not turn.chat_turn_id:
        return
    chat_turn = await runtime.store.get(ChatTurn, turn.chat_turn_id)
    snapshot = chat_turn.request_snapshot
    hooks = [
        NativeHookSnapshot.model_validate(item)
        for item in snapshot.get("hook_snapshots", [])
    ]
    if not hooks:
        return
    hooks = list(
        {item.id: item for item in hooks if event_name in item.manifest.events}.values()
    )
    prior = [
        item
        for item in (
            await runtime.store.list_session_entities(
                NativeHookExecution, chat_turn.session_id
            )
        )
        if item.chat_turn_id == chat_turn.id and item.event_name == event_name
    ]
    ended = event_name in {
        "chat.turn.failed",
        "chat.turn.cancelled",
        "chat.turn.interrupted",
    }
    actor = await actor_id_for(
        runtime.store,
        owner_kind="chat",
        owner_id=chat_turn.session_id,
        chat_session_id=chat_turn.session_id,
    )

    async def observe() -> dict[str, Any]:
        service = WorkspaceProvenanceService(runtime.store)
        workspace = await resolve_workspace(
            runtime.workspace_resolver, chat_turn.engagement_id
        )

        async def begin() -> Any:
            return await service.begin(
                workspace,
                engagement_id=chat_turn.engagement_id,
                scope_kind="turn",
                scope_id=chat_turn.id,
                actor_id=actor,
                owner_kind="chat",
                owner_id=chat_turn.session_id,
                chat_session_id=chat_turn.session_id,
                chat_turn_id=chat_turn.id,
            )

        async def finish() -> Any:
            return await service.finish(
                workspace,
                engagement_id=chat_turn.engagement_id,
                scope_kind="turn",
                scope_id=chat_turn.id,
            )

        if event_name == "chat.turn.started":
            observation = await begin()
        else:
            try:
                observation = await finish()
            except NotFoundError:  # diagnostic-expected: recovered or completion-only hook uses a same-state baseline
                (await begin())
                observation = await finish()
        return service.receipt(observation)

    provenance = await observe()
    for hook in hooks:
        executions = [item for item in prior if item.hook_id == hook.id]
        existing = max(
            executions, key=lambda item: (item.started_at, item.id), default=None
        )
        if existing is not None:
            if existing.status in {"complete", "reconciled"} or (
                existing.status in {"failed", "timed_out"}
                and hook.manifest.failure_policy != "block"
            ):
                continue
            # Never replay an uncertain/effectful guard or silently treat its
            # interruption as permission to start work.
            if not ended:
                raise HarnessHookBlocked(
                    f"Required lifecycle hook {hook.id!r} has saved status {existing.status}. Review its outcome before retrying."
                )
            continue
        result = await NativeHookRunner(runtime.store).run(
            hook,
            engagement_id=chat_turn.engagement_id,
            chat_session_id=chat_turn.session_id,
            chat_turn_id=chat_turn.id,
            owner_kind="chat",
            owner_id=chat_turn.session_id,
            event_name=event_name,
            payload={
                "harness_profile_id": snapshot.get("harness_profile_id"),
                "model": chat_turn.model,
                **(payload or {}),
            },
            workspace_provenance=provenance,
            project_workspace_binding=(
                chat_turn.engagement_id,
                snapshot.get("project_workspace_path"),
            ),
        )
        if (
            result.status != "complete"
            and hook.manifest.failure_policy == "block"
            and not ended
        ):
            raise HarnessHookBlocked(
                f"Required lifecycle hook {hook.id!r} blocked {event_name}: {result.error or result.status}. Review the saved hook outcome before retrying."
            )


async def finish_terminal_hooks(runtime: Any, turn_id: str) -> None:
    """Observe terminal exits even if a start guard or connection failed."""
    from .chat import _turn_end_hook_payload
    from .diagnostics import record_caught_exception

    turn = await runtime.store.get(HarnessTurn, turn_id)
    if not turn.chat_turn_id:
        return
    if turn.status not in {
        HarnessTurnStatus.FAILED,
        HarnessTurnStatus.CANCELLED,
        HarnessTurnStatus.INTERRUPTED,
    }:
        return
    # The shared runner records a late outcome if an in-flight process survives
    # cancellation. Fence that unknown outcome now; never claim it succeeded.
    for execution in await runtime.store.list_session_entities(
        NativeHookExecution, turn.chat_session_id
    ):
        if (
            execution.chat_turn_id == turn.chat_turn_id
            and execution.status == "running"
        ):
            try:
                (
                    await runtime.store.update(
                        NativeHookExecution,
                        execution.id,
                        {
                            "status": "interrupted",
                            "completed_at": utc_now(),
                            "error": "The harness turn ended before the hook outcome was known.",
                        },
                        expected_revision=execution.revision,
                    )
                )
            except (
                ConflictError
            ):  # diagnostic-expected: the late hook outcome won the fence
                pass
    unknown = [
        item.id
        for item in (
            await runtime.store.list_session_entities(
                NativeHookExecution, turn.chat_session_id
            )
        )
        if item.chat_turn_id == turn.chat_turn_id
        and item.side_effects != "none"
        and item.status == "interrupted"
        and not (item.late_outcome and item.late_outcome.status == "complete")
    ]
    if unknown:
        owner = await runtime.store.get(ChatTurn, turn.chat_turn_id)
        recovery = owner.request_snapshot.get("recovery", {})
        (
            await runtime.store.update(
                ChatTurn,
                owner.id,
                {
                    "request_snapshot": {
                        **owner.request_snapshot,
                        "recovery": {
                            **recovery,
                            "required": True,
                            "cause": "harness_hook_interrupted",
                            "unknown_hook_execution_ids": sorted(
                                set(recovery.get("unknown_hook_execution_ids", []))
                                | set(unknown)
                            ),
                        },
                    }
                },
                expected_revision=owner.revision,
            )
        )
    event = (
        "chat.turn.cancelled"
        if turn.status == HarnessTurnStatus.CANCELLED
        else "chat.turn.failed"
    )
    try:
        await run_turn_hooks(
            runtime, turn, event, _turn_end_hook_payload(turn.status.value, turn.error)
        )
    except Exception as exc:
        record_caught_exception(
            "harnesses",
            "harnesses.turn.terminal_hook_failed",
            "A terminal lifecycle hook could not finish. Review its saved outcome.",
            exc,
            stage="turn-cleanup",
            metadata={"entity_type": "harness_turn", "entity_id": turn.id},
        )
