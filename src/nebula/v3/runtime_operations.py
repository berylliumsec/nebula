"""Durable host operation receipts; an unknown effect is never replayed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import os
from uuid import uuid4

from .database import DeploymentOperationRow, RuntimeProcessRow, RuntimeInstanceRow
from .deployment_recovery import _CHECKPOINT
from .process_identity import (
    process_birth,
    process_origin,
    origin_from_previous_boot,
    same_process,
)
from .deployments import DeploymentCoordinator, _checkpoint
from .diagnostics import record_caught_exception
from .runtime_coordination import CoordinationConflict, OperationRejected
from .runtime_ipc import RuntimeIpcError


def _document(value: dict) -> dict:
    value = _checkpoint(value)
    if len(json.dumps(value, sort_keys=True).encode()) > 64 * 1024:
        raise ValueError("deployment operation document exceeds its bound")
    return value


# Operations whose effect is one transaction with an exactly keyed durable
# record. An error after that commit must not leave an unsettleable receipt.
_DURABLE_OUTCOME_ACTIONS = frozenset(
    {"recover_project", "reconcile_project", "promote_api"}
)
_DURABLE_RECORDS = {
    "recover_project": "runtime_recoveries row with this operation ID",
    "reconcile_project": "orphan-recovery deployment receipt for the expected owner",
    "promote_api": "api.promoted deployment receipt for this runtime at expected_revision + 1",
}


async def durable_outcome(session, row) -> dict | None:
    """Return the committed result of ``row``'s effect, or None when absent.

    * ``recover_project``: begin_recovery's RuntimeRecoveryRow carries the
      same operation ID. Attach, verification and activation continue from
      that row; its current phase is reported.
    * ``reconcile_project``: the orphan reconciliation receipt keyed by the
      exact expected owner.
    * ``promote_api``: the ``api.promoted`` receipt for this runtime at the
      single revision this request's expected revision could produce.
    """
    from sqlalchemy import select

    from .database import DeploymentReceiptRow, RuntimeRecoveryRow

    parameters = row.parameters or {}
    if row.action == "recover_project":
        recovery = await session.get(RuntimeRecoveryRow, row.operation_id)
        if recovery is None or recovery.project_id != parameters.get("project_id"):
            return None
        return {
            "project_id": recovery.project_id,
            "runtime_id": recovery.target_runtime_id,
            "fencing_token": recovery.target_fencing_token,
            "recovery_phase": recovery.phase,
            "confirmed_by": "runtime_recovery",
        }
    if row.action == "reconcile_project":
        from .runtime_coordination import ProjectLease
        from .runtime_orphan_recovery import orphan_receipt_key

        owner = parameters.get("expected_owner") or {}
        receipt = await session.get(
            DeploymentReceiptRow, orphan_receipt_key(ProjectLease(**owner))
        )
        if receipt is None:
            return None
        return {**receipt.result, "confirmed_by": "orphan_reconciliation_receipt"}
    if row.action == "promote_api":
        runtime_id = parameters.get("runtime_id")
        revision = parameters.get("expected_revision")
        if not isinstance(runtime_id, str) or type(revision) is not int:
            return None
        for receipt in await session.scalars(
            select(DeploymentReceiptRow).where(
                DeploymentReceiptRow.action == "api.promoted",
                DeploymentReceiptRow.result["runtime_id"].as_string() == runtime_id,
            )
        ):
            if receipt.result.get("revision") == revision + 1:
                return {
                    "api_runtime_id": runtime_id,
                    "version": receipt.result.get("version"),
                    "revision": revision + 1,
                    "confirmed_by": "api_promoted_receipt",
                }
    return None


class DeploymentOperations:
    def __init__(
        self, coordinator: DeploymentCoordinator, *, owner_id: str | None = None
    ):
        self.coordinator = coordinator
        self.owner_id = owner_id or str(uuid4())
        self.active: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _receipt(row):
        return {
            "operation_id": row.operation_id,
            "action": row.action,
            "state": row.state,
            "result": (
                {key: value for key, value in row.result.items() if key != "recovery"}
                if isinstance(row.result, dict)
                else row.result
            ),
        }

    async def execute(self, **arguments) -> dict:
        operation_id = arguments["operation_id"]
        task = asyncio.current_task()
        assert task is not None
        async with self._locks.setdefault(operation_id, asyncio.Lock()):
            owns = operation_id not in self.active
            if owns:
                self.active[operation_id] = task
        try:
            return await self._execute(**arguments)
        finally:
            if owns and self.active.get(operation_id) is task:
                self.active.pop(operation_id, None)

    async def _execute(
        self,
        *,
        operation_id: str,
        actor_id: str,
        action: str,
        parameters: dict,
        dispatch: Callable[[str, dict], Awaitable[dict]],
        retry_rejected: bool = False,
    ) -> dict:
        """Run an operation once per ID; a replayed ID returns its saved receipt.

        ``retry_rejected`` is only for callers whose IDs are deterministic
        (automatic recovery): a rejected receipt proved no effect, so that
        caller may ask again under the same ID, with fresh parameters. The
        earlier refusal is kept as a durable receipt. Uncertain and succeeded
        receipts are never dispatched again.
        """
        for value, name, maximum in (
            (operation_id, "operation_id", 200),
            (actor_id, "actor_id", 200),
            (action, "action", 80),
        ):
            if not value or len(value) > maximum:
                raise ValueError(name + " is missing or exceeds its bound")
        parameters = _document(parameters)
        digest = hashlib.sha256(
            json.dumps(
                [action, parameters], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(DeploymentOperationRow, operation_id)
            now = await self.coordinator.coordination._clock_ms(session)
            if row is not None:
                retry = (
                    retry_rejected
                    and row.state == "failed"
                    and row.actor_id == actor_id
                    and row.action == action
                )
                if not retry and (
                    row.actor_id != actor_id or row.request_hash != digest
                ):
                    raise CoordinationConflict(
                        "operation ID already identifies another request"
                    )
                if row.state == "running" and row.claim_owner != self.owner_id:
                    row.state, row.updated_at_ms = "uncertain", now
                    row.result = {
                        **(row.result or {}),
                        "detail": "The previous host did not confirm this operation. Read deployment status before reconciling it.",
                    }
                if not retry:
                    return self._receipt(row)
                await self.coordinator._receipt(
                    session,
                    "operation.retried",
                    {
                        "operation_id": operation_id,
                        "action": action,
                        "previous_request_hash": row.request_hash,
                        "previous_result": row.result,
                        "previous_updated_at_ms": row.updated_at_ms,
                    },
                )
                row.request_hash, row.parameters = digest, parameters
                row.state, row.claim_owner = "running", self.owner_id
                row.result = {"recovery": self._origin()}
                row.updated_at_ms = now
            else:
                row = DeploymentOperationRow(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    action=action,
                    request_hash=digest,
                    parameters=parameters,
                    state="running",
                    claim_owner=self.owner_id,
                    result={"recovery": self._origin()},
                    created_at_ms=now,
                    updated_at_ms=now,
                )
                session.add(row)

        async def checkpoint(values):
            await self._checkpoint(operation_id, values)

        token = _CHECKPOINT.set(checkpoint)
        try:
            result = _document(await dispatch(action, parameters))
        except BaseException as error:
            _CHECKPOINT.reset(token)
            error_type = type(error).__name__
            rejected = isinstance(error, OperationRejected)
            # The receipt below is what the operator reads; this keeps the
            # failure's type and chain, never its text, in Core diagnostics.
            record_caught_exception(
                "runtime",
                "runtime.deployment_operation.rejected"
                if rejected
                else "runtime.deployment_operation.unconfirmed",
                "A deployment operation was refused before any effect."
                if rejected
                else "A deployment operation did not confirm its result.",
                error,
                stage="deployment",
                metadata={
                    "action": action,
                    "entity_id": operation_id,
                    "kind": "deployment-operation",
                },
            )
            detail = (
                str(error)[:4000]
                if rejected
                else "The operation did not report a confirmed result. Read deployment status before reconciliation."
            )
            # Refusals carry operator-facing text; keep it beside the uncertain
            # outcome so the operator learns why. Other errors may embed
            # private values and stay classified by type only.
            reason = (
                (str(error) or error_type)[:4000]
                if not rejected
                and isinstance(error, (RuntimeIpcError, CoordinationConflict))
                else None
            )

            async def uncertain():
                if not rejected and action in _DURABLE_OUTCOME_ACTIONS:
                    # The effect may have committed before the error. Its own
                    # durable record decides; only a proven effect settles it.
                    await self._finish_unconfirmed(
                        operation_id,
                        {
                            "detail": detail,
                            "error_type": error_type,
                            **({"reason": reason} if reason else {}),
                        },
                    )
                    return
                await self._finish(
                    operation_id,
                    "failed" if rejected else "uncertain",
                    {
                        "detail": detail,
                        "error_type": error_type,
                        **({"reason": reason} if reason else {}),
                    },
                )

            from .async_utils import finish_cleanup

            await finish_cleanup(uncertain())
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            return await self.status(operation_id, actor_id=actor_id)
        else:
            _CHECKPOINT.reset(token)
        await self._finish(operation_id, "succeeded", result)
        return await self.status(operation_id, actor_id=actor_id)

    def _origin(self) -> dict:
        return {
            "format": "deployment-recovery/v1",
            "owner": self.owner_id,
            "pid": os.getpid(),
            "birth": process_birth(os.getpid()),
            "process_origin": process_origin(),
            "phase": "dispatch",
            "children": [],
        }

    async def _checkpoint(self, operation_id: str, values: dict) -> None:
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(DeploymentOperationRow, operation_id)
            if (
                row is None
                or row.claim_owner != self.owner_id
                or row.state != "running"
            ):
                raise CoordinationConflict(
                    "operation ownership changed before effect admission"
                )
            recovery = dict((row.result or {}).get("recovery") or {})
            recovery.update(values)
            row.result = {"recovery": recovery}
            row.updated_at_ms = await self.coordinator.coordination._clock_ms(session)

    async def _finish(self, operation_id, state, result):
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(DeploymentOperationRow, operation_id)
            if (
                row is None
                or row.claim_owner != self.owner_id
                or row.state != "running"
            ):
                raise CoordinationConflict(
                    "operation ownership changed before completion"
                )
            recovery = (row.result or {}).get("recovery")
            if recovery:
                recovery = {**recovery, "dispatch_finished": True}
            row.state, row.result = (
                state,
                {**result, **({"recovery": recovery} if recovery else {})},
            )
            row.updated_at_ms = await self.coordinator.coordination._clock_ms(session)

    async def _finish_unconfirmed(self, operation_id, failure: dict) -> None:
        """Record an unconfirmed dispatch from its effect's durable record.

        A proven commit is ``succeeded`` with that record, keeping the dispatch
        error type beside it. Absence alone does not prove that a lost COMMIT
        did nothing, so that receipt stays ``uncertain`` and names the record
        reconciliation reads.
        """
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(
                DeploymentOperationRow, operation_id, with_for_update=True
            )
            if (
                row is None
                or row.claim_owner != self.owner_id
                or row.state != "running"
            ):
                raise CoordinationConflict(
                    "operation ownership changed before completion"
                )
            outcome = await durable_outcome(session, row)
            recovery = (row.result or {}).get("recovery")
            if recovery:
                recovery = {**recovery, "dispatch_finished": True}
            if outcome is not None:
                row.state = "succeeded"
                result = {
                    **outcome,
                    "dispatch_error_type": failure["error_type"],
                    "detail": "The effect committed before the reply failed; this result is read from its durable record.",
                }
            else:
                row.state = "uncertain"
                result = {**failure, "durable_record": _DURABLE_RECORDS[row.action]}
            row.result = {**result, **({"recovery": recovery} if recovery else {})}
            row.updated_at_ms = await self.coordinator.coordination._clock_ms(session)

    async def reconcile(self, operation_id: str, *, actor_id: str) -> dict:
        """Resolve only an inactive, durably linked operation; never replay it."""
        if self._locks.setdefault(operation_id, asyncio.Lock()).locked():
            raise CoordinationConflict(
                "The original deployment operation is still active"
            )
        async with self._locks[operation_id]:
            return await self._reconcile(operation_id, actor_id=actor_id)

    async def _reconcile(self, operation_id: str, *, actor_id: str) -> dict:
        task = self.active.get(operation_id)
        if task is not None and not task.done():
            raise CoordinationConflict(
                "The original deployment operation is still active"
            )
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(
                DeploymentOperationRow, operation_id, with_for_update=True
            )
            if row is None or row.actor_id != actor_id:
                raise CoordinationConflict("deployment operation is unavailable")
            if row.state in {"failed", "succeeded"}:
                return self._receipt(row)
            previous = dict(row.result or {})
            recovery = previous.get("recovery") or {}
            if recovery.get("format") != "deployment-recovery/v1":
                raise CoordinationConflict(
                    "Legacy operation has no durable effect identity; retain its uncertain receipt"
                )
            current_origin = process_origin()
            recorded_origin = recovery.get("process_origin")
            previous_boot = origin_from_previous_boot(recorded_origin, current_origin)
            if (
                current_origin is None
                or recorded_origin is None
                or (recorded_origin != current_origin and not previous_boot)
            ):
                raise CoordinationConflict(
                    "Operation process namespace is unknown or foreign"
                )
            same_owner = recovery.get("owner") == self.owner_id
            if not same_owner:
                pid, birth = recovery.get("pid"), recovery.get("birth")
                if (
                    not isinstance(pid, int)
                    or not isinstance(birth, str)
                    or (not previous_boot and same_process(pid, birth))
                ):
                    raise CoordinationConflict(
                        "The original operation host is not confirmed gone"
                    )
            phase = recovery.get("phase")
            if (
                phase == "dispatch"
                and not same_owner
                and row.action != "retire"
                and row.action not in _DURABLE_OUTCOME_ACTIONS
            ):
                raise CoordinationConflict(
                    "Previous host did not publish an effect boundary"
                )
            pre_admission = phase in {"migration_spawn_intent", "worker_spawn_intent"}
            children = recovery.get("children")
            if not isinstance(children, list):
                raise CoordinationConflict("Operation child inventory is unavailable")
            for child in children:
                if (
                    not isinstance(child, dict)
                    or not isinstance(child.get("pid"), int)
                    or not isinstance(child.get("birth"), str)
                ):
                    raise CoordinationConflict("Operation child identity is incomplete")
                child_origin = child.get("process_origin")
                child_previous_boot = origin_from_previous_boot(
                    child_origin, current_origin
                )
                if child_origin is None or (
                    child_origin != current_origin and not child_previous_boot
                ):
                    raise CoordinationConflict(
                        "Operation child namespace is unknown or foreign"
                    )
                # Ready workers are the effect; migrations must have exited.
                if (
                    not child_previous_boot
                    and child.get("kind") != "worker"
                    and same_process(child["pid"], child["birth"])
                ):
                    raise CoordinationConflict(
                        "The original migration child is still running"
                    )
            result, state = None, None
            if pre_admission:
                # Child stdin receives no bootstrap until the bound-child
                # checkpoint replaces intent. A dead parent cannot admit it.
                if same_owner and recovery.get("dispatch_finished") is not True:
                    raise CoordinationConflict("Spawn admission has not finished")
                if row.action in {"prepare_api", "prepare_execution"}:
                    runtime_id = recovery.get("runtime_id")
                    process = (
                        await session.get(RuntimeProcessRow, runtime_id)
                        if isinstance(runtime_id, str)
                        else None
                    )
                    if process is not None and process.state != "retired":
                        raise CoordinationConflict(
                            "Unadmitted worker reservation needs verified abort"
                        )
                state = "failed"
            elif row.action in _DURABLE_OUTCOME_ACTIONS:
                # One transaction with an exactly keyed record. Its original
                # host is confirmed gone, or this host finished the dispatch,
                # so a commit that is absent now can no longer appear.
                result = await durable_outcome(session, row)
                if result is not None:
                    state = "succeeded"
                elif not same_owner or recovery.get("dispatch_finished") is True:
                    state = "failed"
            elif row.action == "retire":
                identity = await self.coordinator._retirement_readback(
                    session, row.parameters["runtime_id"]
                )
                if identity is not None:
                    result, state = identity.model_dump(mode="json"), "succeeded"
            elif row.action in {"prepare_api", "prepare_execution"}:
                runtime_id = recovery.get("runtime_id")
                if isinstance(runtime_id, str):
                    process = await session.get(RuntimeProcessRow, runtime_id)
                    linked = [
                        child for child in children if child.get("kind") == "worker"
                    ]
                    runtime = await session.get(RuntimeInstanceRow, runtime_id)
                    now = await self.coordinator.coordination._clock_ms(session)
                    if (
                        process is not None
                        and process.version == row.parameters.get("version")
                        and process.role
                        == ("api" if row.action == "prepare_api" else "execution")
                        and process.state == "ready"
                        and process.endpoint == recovery.get("endpoint")
                        and runtime is not None
                        and runtime.lease_expires_at_ms > now
                        and len(linked) == 1
                        and linked[0].get("process_origin") == current_origin
                        and (process.pid, process.process_birth)
                        == (linked[0]["pid"], linked[0]["birth"])
                        and same_process(process.pid, process.process_birth)
                    ):
                        result = {"runtime_id": runtime_id, "version": process.version}
                        state = "succeeded"
                    elif process is not None and process.state == "retired":
                        state = "failed"
                elif (
                    same_owner
                    and recovery.get("dispatch_finished") is True
                    and not children
                    and recovery.get("phase") == "dispatch"
                ):
                    state = "failed"
            elif row.action == "migrate":
                from sqlalchemy import text
                from .database import RuntimeReleaseRow

                release = await session.get(
                    RuntimeReleaseRow, row.parameters["version"]
                )
                heads = list(
                    await session.scalars(
                        text("SELECT version_num FROM alembic_version")
                    )
                )
                target = (
                    release.manifest.get("migration_head")
                    if release is not None
                    else None
                )
                if heads == [target] and isinstance(target, str):
                    result = {
                        "version": row.parameters["version"],
                        "current_head": target,
                        "target_head": target,
                        "state": "current",
                    }
                    state = "succeeded"
                elif (
                    same_owner
                    and recovery.get("dispatch_finished") is True
                    and recovery.get("phase") == "dispatch"
                    and not children
                ):
                    state = "failed"
            if state is None:
                raise CoordinationConflict(
                    "Operation effect is not fully reconciled; retain its uncertain receipt"
                )
            await self.coordinator._receipt(
                session,
                "operation.reconciled",
                {
                    "operation_id": operation_id,
                    "previous_state": row.state,
                    "previous_result": previous,
                    "state": state,
                },
            )
            row.state = state
            row.result = {
                **(
                    result
                    or {
                        "detail": "No admitted effect remains",
                        "error_type": "OperationRejected",
                    }
                ),
                "recovery": recovery,
                "reconciled": True,
            }
            row.updated_at_ms = await self.coordinator.coordination._clock_ms(session)
            return self._receipt(row)

    async def status(self, operation_id: str, *, actor_id: str) -> dict:
        async with self.coordinator.database.session() as session:
            row = await session.get(DeploymentOperationRow, operation_id)
            if row is None or row.actor_id != actor_id:
                raise CoordinationConflict("deployment operation is unavailable")
        async with self.coordinator.coordination._write() as session:
            await self.coordinator._host(session)
            row = await session.get(
                DeploymentOperationRow, operation_id, with_for_update=True
            )
            if row is None or row.actor_id != actor_id:
                raise CoordinationConflict("deployment operation is unavailable")
            if row.state == "running" and row.claim_owner != self.owner_id:
                row.state = "uncertain"
                row.updated_at_ms = await self.coordinator.coordination._clock_ms(
                    session
                )
                row.result = {
                    **(row.result or {}),
                    "detail": "The previous host did not confirm this operation. Read deployment status before reconciling it.",
                }
            return self._receipt(row)
