"""Butler changes from its home worker, run by the target project's own worker.

Butler's home worker reads other projects but may not write them: its SQL
scope fences every write to ``BUTLER_HOME_PROJECT``. A change Butler makes to
another project travels as one ``butler.action/v1`` command. The home worker
asks the host to submit it (only the host writes ``runtime_commands``); the
target project's owner runs it on its control lane through the same Core
action Butler uses inside that project, and records the outcome. See
docs/design/butler-home-worker.md, section 4.

Three rules keep this safe:

* Only the worker holding the home project's lease may submit or read these
  commands, and only for actions in the closed set of Butler work changes.
  The one exception is a Nebula-wide scheduled check (``initiator:
  "background"``): the worker of the Act project hosting it may send its
  changes to other projects while the host reads that case as investigating
  in the sender's project. The target then checks its own Butler authority:
  it runs the change only under current Act authority at the revision Butler
  read, with Work enabled and no hold.
* The host submits only to a project whose every possible worker declares
  ``butler.action/v1``. An older worker would mark the unknown command
  uncertain, and an uncertain head blocks that project's control lane,
  including Stop and approvals.
* The target handler always finishes a command as succeeded or failed, never
  uncertain, and refuses one that expired while it queued.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select

from .database import (
    DeploymentHostRow,
    EntityRow,
    ProjectHandoffRow,
    ProjectOwnershipRow,
    RuntimeCommandRow,
    RuntimeProcessRow,
    RuntimeReleaseRow,
)
from .deployment_protocol import (
    BUTLER_ACTION_COMMAND,
    BUTLER_HOME_PROJECT,
    RESERVED_PROJECT_IDS,
    ReleaseManifest,
)
from .diagnostics import record_caught_exception
from .domain import (
    ButlerAuthorityPolicy,
    ButlerRecoveryCase,
    Engagement,
    EngagementStatus,
    NebulaModel,
)
from .runtime_coordination import (
    ClaimedCommand,
    CoordinationConflict,
    LeaseLost,
    ProjectLease,
)
from .shared_memory_rpc import MemoryAuthority
from .stored_entities import decode_stored

# A release that declares this can run the command and its handler.
BUTLER_ACTION_FEATURE = BUTLER_ACTION_COMMAND
BUTLER_SUBMIT_ACTION = "butler.submit-action/v1"
BUTLER_ACTION_RESULT = "butler.action-result/v1"
BUTLER_ACTOR = "butler"
# Long enough to wait out a busy project; short enough that a change Butler
# reported as accepted never lands hours later.
ACTION_TTL_MS = 10 * 60 * 1000
RESULT_WAIT_SECONDS = 30.0


def butler_work_changes() -> frozenset[str]:
    """The Butler work actions that change a project: the only ones sent."""

    from .control_assistant_work import _ALL_WORK_TOOLS

    return frozenset(
        name
        for name, _description, _schema, read_only in _ALL_WORK_TOOLS
        if not read_only
    )


class ButlerAction(NebulaModel):
    action: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # "operator": Butler acting because the operator asked in a Butler
    # thread. "background": a Nebula-wide scheduled check's own change, which
    # the target checks against its Butler authority at authority_revision.
    initiator: Literal["operator", "background"] = "operator"
    home_session_id: str | None = Field(default=None, max_length=200)
    home_turn_id: str | None = Field(default=None, max_length=200)
    case_id: str | None = Field(default=None, min_length=1, max_length=200)
    authority_revision: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def background_names_its_case(self) -> "ButlerAction":
        background = self.initiator == "background"
        if background != (self.case_id is not None) or background != (
            self.authority_revision is not None
        ):
            raise ValueError(
                "a background Butler change names its case and authority revision"
            )
        return self


class ButlerActionCommand(ButlerAction):
    expires_at_ms: int = Field(ge=1, strict=True)


class ButlerSubmitRequest(NebulaModel):
    lease: MemoryAuthority
    # Not "*_id": routing treats those fields as project references.
    target_project: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    action: ButlerAction


class ButlerResultRequest(NebulaModel):
    lease: MemoryAuthority
    target_project: str = Field(min_length=1, max_length=200)
    command_id: str = Field(min_length=1, max_length=200)
    # Set when a Nebula-wide check's host worker reads its own case's change.
    case_id: str | None = Field(default=None, min_length=1, max_length=200)


GLOBAL_CHECK_SIGNAL = "butler.global_periodic_check"


def action_key(turn_id: str | None, action: str, arguments: dict[str, Any]) -> str:
    """One command per turn, action and exact arguments.

    A retried tool call in the same turn finds the first command; the same
    change asked for in a later turn is a new request.
    """

    digest = hashlib.sha256(
        json.dumps(
            [turn_id or "", action, arguments], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return f"butler-action:{digest[:48]}"


async def _release_supports(session, version: str | None) -> bool:
    if version is None:
        return False
    release = await session.get(RuntimeReleaseRow, version)
    return (
        release is not None
        and BUTLER_ACTION_FEATURE
        in ReleaseManifest.model_validate(release.manifest).features
    )


async def butler_actions_supported(session, project_id: str, *, now_ms: int) -> bool:
    """Whether every worker that may run this project's next command can run it.

    That is the live owner, the release a new owner starts from, and the
    target of a handoff still in flight. A host with no promoted target starts
    owners from its own release, which registered this submission path.
    """

    owner = await session.get(ProjectOwnershipRow, project_id)
    if owner is not None and owner.runtime_id and owner.lease_expires_at_ms > now_ms:
        process = await session.get(RuntimeProcessRow, owner.runtime_id)
        if not await _release_supports(
            session, process.version if process is not None else None
        ):
            return False
    host = await session.get(DeploymentHostRow, "host")
    target = host.target_version if host is not None else None
    if target is not None and not await _release_supports(session, target):
        return False
    handoff = await session.get(ProjectHandoffRow, project_id)
    if handoff is not None and handoff.phase != "verified":
        if not await _release_supports(session, handoff.target_version):
            return False
    return True


def register_butler_actions(rpc, runtime_id: str, coordination) -> None:
    """Let only Butler's home worker submit and read its own action commands."""

    async def _home(
        session,
        raw: MemoryAuthority,
        target: str,
        case_id: str | None = None,
        *,
        investigating: bool = True,
    ) -> ProjectLease:
        lease = ProjectLease(**raw.model_dump())
        if lease.runtime_id != runtime_id:
            raise ValueError("Butler actions require the sender's own grant")
        if case_id is None and lease.project_id != BUTLER_HOME_PROJECT:
            raise ValueError("Butler actions require the Butler home grant")
        if target in RESERVED_PROJECT_IDS:
            raise ValueError("Butler actions target an operator project")
        owned = await session.get(ProjectOwnershipRow, lease.project_id)
        if (
            owned is None
            or owned.runtime_id != lease.runtime_id
            or owned.fencing_token != lease.fencing_token
        ):
            raise ValueError("Butler's grant is stale")
        if case_id is not None:
            # A Nebula-wide check's host worker sends only its own case's
            # changes, and only to other projects (its own it changes itself).
            row = await session.get(EntityRow, case_id)
            case = (
                decode_stored(ButlerRecoveryCase, row.payload)
                if row is not None and row.kind == ButlerRecoveryCase.entity_kind
                else None
            )
            if (
                case is None
                or target == lease.project_id
                or lease.project_id in RESERVED_PROJECT_IDS
                or case.engagement_id != lease.project_id
                or case.signal != GLOBAL_CHECK_SIGNAL
                or (investigating and case.status != "investigating")
            ):
                raise ValueError("Butler's scheduled check cannot send this change")
        return lease

    async def submit(payload):
        try:
            request = ButlerSubmitRequest.model_validate(payload)
            if request.action.action not in butler_work_changes():
                raise ValueError("Butler can send only its listed work changes")
            async with coordination.database.session() as session:
                await _home(
                    session,
                    request.lease,
                    request.target_project,
                    request.action.case_id,
                )
                # A resend (lost response, retried tool call) finds the first
                # command. Its expiry was stamped then, so re-submitting would
                # change the payload and be refused as a different request.
                existing = await session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.actor_id == BUTLER_ACTOR,
                        RuntimeCommandRow.idempotency_key == request.idempotency_key,
                    )
                )
                now_ms = await coordination._clock_ms(session)
            if existing is not None:
                if (
                    existing.project_id != request.target_project
                    or existing.name != BUTLER_ACTION_COMMAND
                    or (existing.payload or {}).get("action") != request.action.action
                    or (existing.payload or {}).get("arguments")
                    != request.action.arguments
                ):
                    raise CoordinationConflict(
                        "This Butler request was already sent with other details"
                    )
                return {"command_id": existing.id}
            command = ButlerActionCommand(
                **request.action.model_dump(), expires_at_ms=now_ms + ACTION_TTL_MS
            )

            async def admit(session):
                now = await coordination._clock_ms(session)
                if not await butler_actions_supported(
                    session, request.target_project, now_ms=now
                ):
                    raise CoordinationConflict(
                        "This project's Core worker cannot take Butler's changes "
                        "yet; it will after its next update."
                    )

            receipt = await coordination.submit(
                project_id=request.target_project,
                actor_id=BUTLER_ACTOR,
                idempotency_key=request.idempotency_key,
                name=BUTLER_ACTION_COMMAND,
                payload=command.model_dump(mode="json"),
                lane="control",
                admit=admit,
            )
            return {"command_id": receipt.command_id}
        except CoordinationConflict as refusal:
            # diagnostic-expected: a typed refusal (retiring, draining, older worker) reaches Butler as its answer.
            raise HTTPException(409, str(refusal)) from None
        except ValueError:
            # No other project's state leaks through an unauthorised grant.
            raise HTTPException(
                403, "Butler actions are unavailable for this grant"
            ) from None

    async def result(payload):
        try:
            request = ButlerResultRequest.model_validate(payload)
            async with coordination.database.session() as session:
                await _home(
                    session,
                    request.lease,
                    request.target_project,
                    request.case_id,
                    investigating=False,
                )
                row = await session.scalar(
                    select(RuntimeCommandRow).where(
                        RuntimeCommandRow.id == request.command_id,
                        RuntimeCommandRow.project_id == request.target_project,
                        RuntimeCommandRow.actor_id == BUTLER_ACTOR,
                        RuntimeCommandRow.name == BUTLER_ACTION_COMMAND,
                    )
                )
            if row is None or (row.payload or {}).get("case_id") != request.case_id:
                raise ValueError("unknown Butler action")
            return {"state": row.state, "result": row.result}
        except ValueError:
            # diagnostic-expected: an unauthorised or unknown read gets one refusal shape.
            raise HTTPException(
                403, "Butler actions are unavailable for this grant"
            ) from None

    rpc.register(BUTLER_SUBMIT_ACTION, submit)
    rpc.register(BUTLER_ACTION_RESULT, result)


class RemoteButlerActions:
    """The home worker's client: submit one change, then wait for its outcome."""

    def __init__(self, client, lease: ProjectLease, *, poll_seconds: float = 0.5):
        self.client, self.lease, self.poll_seconds = client, lease, poll_seconds

    async def run(
        self,
        target_project: str,
        action: ButlerAction,
        *,
        wait_seconds: float = RESULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        authority = MemoryAuthority(**asdict(self.lease))
        submitted = await self.client.call(
            BUTLER_SUBMIT_ACTION,
            ButlerSubmitRequest(
                lease=authority,
                target_project=target_project,
                idempotency_key=action_key(
                    # A scheduled check may send one change to several
                    # projects; each target gets its own command.
                    action.home_turn_id
                    if action.initiator == "operator"
                    else f"{action.case_id}\0{target_project}",
                    action.action,
                    action.arguments,
                ),
                action=action,
            ).model_dump(mode="json"),
        )
        command_id = str(submitted["command_id"])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while True:
            outcome = await self.client.call(
                BUTLER_ACTION_RESULT,
                ButlerResultRequest(
                    lease=authority,
                    target_project=target_project,
                    command_id=command_id,
                    case_id=action.case_id,
                ).model_dump(mode="json"),
            )
            state = outcome.get("state")
            if state in {"succeeded", "failed"}:
                return {
                    "state": state,
                    "command_id": command_id,
                    "result": outcome.get("result") or {},
                }
            if loop.time() >= deadline:
                # Still queued behind the project's other work: it runs there
                # unless it expires first, and a later read reports it.
                return {"state": "accepted", "command_id": command_id, "result": {}}
            await asyncio.sleep(self.poll_seconds)


async def _check_background_authority(
    store, project: Engagement, request: ButlerActionCommand
) -> None:
    """A scheduled check's change runs only under this project's current Act."""

    from .control_assistant_work import HOLD_OPERATIONS
    from .project_hold_gate import active_hold, held_detail

    policy = await store.find_entity(ButlerAuthorityPolicy, "engagement_id", project.id)
    authority = policy.authority if policy is not None else "watch"
    if authority != "act":
        raise ValueError(
            f"Butler has {authority.capitalize()} authority here; a scheduled "
            "check changes a project only under Act authority."
        )
    if policy is None or policy.revision != request.authority_revision:
        raise ValueError("Butler authority here changed; read the project again.")
    if not project.work_enabled:
        raise ValueError("Work is turned off in this project.")
    if request.action in HOLD_OPERATIONS:
        raise ValueError("only the operator's Butler can hold or release work")
    hold = await active_hold(store, project.id)
    if hold is not None:
        raise ValueError(held_detail(hold))


async def run_butler_action(
    app, coordination, lease: ProjectLease, command: ClaimedCommand
) -> None:
    """Run one Butler change in this project's worker; never leave it uncertain."""

    command_id = command.receipt.command_id
    outcome: dict[str, Any]
    state: Literal["succeeded", "failed"]
    try:
        request = ButlerActionCommand.model_validate(command.payload)
        async with coordination.fenced(lease) as session:
            now_ms = await coordination._clock_ms(session)
        if now_ms > request.expires_at_ms:
            raise ValueError(
                "Butler's change waited too long behind this project's work and "
                "was not applied; ask again if it is still wanted."
            )
        if request.action not in butler_work_changes():
            raise ValueError("Butler can send only its listed work changes")
        work = app.state.control_assistant_work
        project = await work.store.get(Engagement, lease.project_id)
        if project.status == EngagementStatus.ARCHIVED:
            raise ValueError("Restore the project before Butler changes it")
        if request.initiator == "background":
            await _check_background_authority(work.store, project, request)
        from .control_assistant_work import dispatch_work_tool

        applied = await dispatch_work_tool(
            work,
            lease.project_id,
            request.action,
            request.arguments,
            caller_session_id=request.home_session_id,
        )
        state = "succeeded"
        outcome = {"action": request.action, "applied": applied}
    except LeaseLost:
        # The new owner's recovery settles this command; never finish it here.
        raise
    except Exception as error:  # noqa: BLE001
        record_caught_exception(
            "butler",
            "butler.home_action.failed",
            "A Butler change from Butler's own worker was refused or failed.",
            error,
            stage="butler-action",
            metadata={"entity_id": command_id, "kind": "runtime-command"},
        )
        state = "failed"
        outcome = {"detail": str(error)[:1000] or type(error).__name__}
    await coordination.finish_command(lease, command_id, state=state, result=outcome)
