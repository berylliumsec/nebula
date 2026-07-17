"""Zero-setup bootstrap and truthful local readiness reporting."""

from __future__ import annotations

from .diagnostics import (
    create_diagnostic_task,
    gather_diagnostic,
    record_caught_exception,
)

import asyncio
import hashlib
import os
import platform as host_platform
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import func, insert, select, update

from .database import (
    BootstrapStateRow,
    EntityRow,
    SCRATCH_PROJECT_BOOTSTRAP_KEY,
)
from .domain import (
    Engagement,
    EngagementStatus,
    NebulaModel,
    ProviderProfile,
    RunnerIsolation,
    RunnerProfile as StoredRunnerProfile,
    RunnerRuntime,
    StringEnum,
    utc_now,
)
from .sandbox import (
    ContainerSandboxRunner,
    RunnerIsolationMode,
    RunnerProfile as SandboxRunnerProfile,
)
from .storage import ConflictError, NebulaStore, NotFoundError
from .runtime_platform import RuntimePlatform, RuntimePlatformError

SCRATCH_PROJECT_ID = "scratch-project"
SCRATCH_PROJECT_NAME = "Scratch Project"
DEFAULT_SETUP_EVENT_RETENTION = 256
MAX_SETUP_EVENT_RETENTION = 10_000


class CoreSetupState(StringEnum):
    READY = "ready"
    DEGRADED = "degraded"
    ERROR = "error"


class TerminalSetupState(StringEnum):
    DETECTING_RUNNER = "detecting_runner"
    NEEDS_RUNNER = "needs_runner"
    PREPARING_IMAGE = "preparing_image"
    READY = "ready"
    DISABLED = "disabled"
    ERROR = "error"


class AssistantSetupState(StringEnum):
    NEEDS_MODEL = "needs_model"
    CONFIGURED = "configured"
    ERROR = "error"


class RunnerCandidateSource(StringEnum):
    CONFIGURED = "configured"
    DETECTED = "detected"


class ImagePreparationPhase(StringEnum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RESOLVING_RUNTIME = "resolving_runtime"
    PREPARING_IMAGE = "preparing_image"
    READY = "ready"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    ERROR = "error"


class SetupEventReason(StringEnum):
    SNAPSHOT = "snapshot"
    RUNTIME_DETECTION_STARTED = "runtime_detection_started"
    RUNTIME_DETECTION_COMPLETED = "runtime_detection_completed"
    RUNNER_SELECTED = "runner_selected"
    IMAGE_PREPARATION_QUEUED = "image_preparation_queued"
    IMAGE_PREPARATION_PROGRESS = "image_preparation_progress"
    IMAGE_PREPARATION_READY = "image_preparation_ready"
    IMAGE_PREPARATION_CANCELLING = "image_preparation_cancelling"
    IMAGE_PREPARATION_CANCELLED = "image_preparation_cancelled"
    IMAGE_PREPARATION_ERROR = "image_preparation_error"


class CoreSetupStatus(NebulaModel):
    status: CoreSetupState
    detail: str | None = None


class RunnerCandidate(NebulaModel):
    candidate_id: str | None = Field(default=None, pattern=r"^fixed:[0-9a-f]{32}$")
    runner_profile_id: str | None = None
    source: RunnerCandidateSource
    name: str
    runtime: RunnerRuntime
    executable: str
    context: str | None = None
    platform: Literal["linux/amd64", "linux/arm64"]
    isolation: RunnerIsolation
    healthy: bool
    detail: str | None = None


class ImagePreparationStatus(NebulaModel):
    phase: ImagePreparationPhase = cast(
        ImagePreparationPhase, ImagePreparationPhase.NOT_STARTED
    )
    operation_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    )
    project_id: str | None = Field(default=None, min_length=1, max_length=200)
    progress_percent: int | None = Field(default=None, ge=0, le=100)
    progress_indeterminate: bool = False
    can_cancel: bool = False
    can_retry: bool = False
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    started_at: datetime | None = None
    completed_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=2_000)


class TerminalSetupStatus(NebulaModel):
    status: TerminalSetupState
    runner_profile_id: str | None = None
    candidates: list[RunnerCandidate] = Field(default_factory=list)
    image_preparation: ImagePreparationStatus = Field(
        default_factory=ImagePreparationStatus
    )
    detail: str | None = None


class AssistantSetupStatus(NebulaModel):
    status: AssistantSetupState
    provider_profile_id: str | None = None
    detail: str | None = None


class SetupStatus(NebulaModel):
    core: CoreSetupStatus
    scratch_project_id: str | None = None
    terminal: TerminalSetupStatus
    assistant: AssistantSetupStatus


class RunnerSelectionRequest(NebulaModel):
    candidate_id: str = Field(pattern=r"^fixed:[0-9a-f]{32}$")


class ImagePreparationRequest(NebulaModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=200)


class ImagePreparationCancellationRequest(NebulaModel):
    operation_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )


class SetupControlResponse(NebulaModel):
    operation: Literal[
        "runner_selection",
        "image_preparation",
        "image_preparation_retry",
        "image_preparation_cancellation",
    ]
    accepted: bool
    idempotent: bool
    operation_id: str | None = Field(
        default=None,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    )
    setup: SetupStatus


class SetupEvent(NebulaModel):
    sequence: int = Field(ge=1)
    occurred_at: datetime
    reason: SetupEventReason
    snapshot: SetupStatus


class SetupEventReplay(NebulaModel):
    events: tuple[SetupEvent, ...]
    oldest_sequence: int = Field(ge=1)
    latest_sequence: int = Field(ge=0)
    truncated: bool


class SetupServiceError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code = code
        self.status_code = status_code


def bootstrap_scratch_project(store: NebulaStore) -> str | None:
    """Consume the durable first-run marker and optionally create Scratch.

    The marker and entity write share one transaction. This makes the operation
    safe to retry and ensures deleting Scratch later cannot make it reappear.
    Any pre-existing entity suppresses creation because that database is not
    truly empty from the user's perspective (notably during a legacy import).
    """

    connection = store.database.engine.connect()
    try:
        if store.database.engine.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        marker = (
            connection.execute(
                select(BootstrapStateRow)
                .where(BootstrapStateRow.key == SCRATCH_PROJECT_BOOTSTRAP_KEY)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if marker is None:
            # A store constructed without the normal Database bootstrap is not
            # eligible. Fail closed instead of guessing that an existing DB is new.
            now = utc_now()
            connection.execute(
                insert(BootstrapStateRow).values(
                    key=SCRATCH_PROJECT_BOOTSTRAP_KEY,
                    status="complete",
                    engagement_id=None,
                    created_at=now,
                    completed_at=now,
                )
            )
            connection.commit()
            return None

        if marker["status"] != "eligible":
            existing_engagement_id = marker["engagement_id"]
            if existing_engagement_id is None:
                connection.commit()
                return None
            exists_now = connection.scalar(
                select(func.count(EntityRow.id)).where(
                    EntityRow.id == existing_engagement_id,
                    EntityRow.kind == Engagement.entity_kind,
                )
            )
            connection.commit()
            return existing_engagement_id if exists_now else None

        entity_count = int(connection.scalar(select(func.count(EntityRow.id))) or 0)
        engagement_id: str | None = None
        if entity_count == 0:
            scratch = Engagement(
                id=SCRATCH_PROJECT_ID,
                name=SCRATCH_PROJECT_NAME,
                description="A local workspace ready for terminal testing.",
                status=EngagementStatus.ACTIVE,
                metadata={
                    "created_by": "system:bootstrap",
                    "bootstrap_kind": SCRATCH_PROJECT_BOOTSTRAP_KEY,
                },
            )
            payload = scratch.model_dump(mode="json")
            connection.execute(
                insert(EntityRow).values(
                    id=scratch.id,
                    kind=scratch.entity_kind,
                    engagement_id=scratch.id,
                    revision=scratch.revision,
                    payload=payload,
                    created_at=scratch.created_at,
                    updated_at=scratch.updated_at,
                )
            )
            engagement_id = scratch.id

        now = utc_now()
        connection.execute(
            update(BootstrapStateRow)
            .where(
                BootstrapStateRow.key == SCRATCH_PROJECT_BOOTSTRAP_KEY,
                BootstrapStateRow.status == "eligible",
            )
            .values(
                status="complete",
                engagement_id=engagement_id,
                completed_at=now,
            )
        )
        connection.commit()
        return engagement_id
    except Exception as caught_error:
        record_caught_exception(
            "setup",
            "setup.setup.caught_failure_001",
            "A handled setup operation raised an exception.",
            caught_error,
            stage="setup",
        )
        connection.rollback()
        raise
    finally:
        connection.close()


def current_scratch_project_id(store: NebulaStore) -> str | None:
    """Return the live bootstrapped project, not a stale deleted marker target."""

    with store.database.session() as session:
        marker = session.get(BootstrapStateRow, SCRATCH_PROJECT_BOOTSTRAP_KEY)
        if marker is None or not marker.engagement_id:
            return None
        exists_now = session.scalar(
            select(func.count(EntityRow.id)).where(
                EntityRow.id == marker.engagement_id,
                EntityRow.kind == Engagement.entity_kind,
            )
        )
        return marker.engagement_id if exists_now else None


class SetupService:
    """Own background runtime detection without blocking Core availability."""

    def __init__(
        self,
        store: NebulaStore,
        tool_platform: RuntimePlatform | None,
        *,
        event_retention: int = DEFAULT_SETUP_EVENT_RETENTION,
    ) -> None:
        if event_retention < 1 or event_retention > MAX_SETUP_EVENT_RETENTION:
            raise ValueError(
                "setup event retention must be between 1 and "
                f"{MAX_SETUP_EVENT_RETENTION}"
            )
        self.store = store
        self.tool_platform = tool_platform
        self._refresh_lock = asyncio.Lock()
        self._preparation_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._preparation_task: asyncio.Task[None] | None = None
        self._image_preparation = ImagePreparationStatus()
        self._event_retention = event_retention
        self._events: deque[SetupEvent] = deque(maxlen=event_retention)
        self._next_event_sequence = 1
        self._event_subscribers: set[asyncio.Queue[SetupEvent]] = set()
        if tool_platform is None or not tool_platform.execution_enabled:
            self._terminal = TerminalSetupStatus(
                status=TerminalSetupState.DISABLED,
                image_preparation=self._image_preparation,
                detail="Container terminal execution is disabled in this Core.",
            )
        else:
            self._terminal = TerminalSetupStatus(
                status=TerminalSetupState.DETECTING_RUNNER,
                image_preparation=self._image_preparation,
                detail="Checking supported local container runtimes.",
            )
        self._emit(SetupEventReason.SNAPSHOT)

    def start(self) -> None:
        if self.tool_platform is None or not self.tool_platform.execution_enabled:
            return
        if self._image_preparation.operation_id is not None:
            return
        if self._refresh_task is None:
            self._refresh_task = create_diagnostic_task(
                self._refresh(),
                feature="setup",
                event_code="setup.runtime_refresh",
                failure_message="Background runner detection stopped unexpectedly.",
                name="nebula-setup-runtime-refresh",
            )

    async def shutdown(self) -> None:
        tasks = [self._refresh_task, self._preparation_task]
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await gather_diagnostic(
                *pending,
                feature="setup",
                event_code="setup.shutdown.task_failed",
                failure_message="A setup background task failed during shutdown.",
                stage="shutdown",
            )

    async def status(self) -> SetupStatus:
        self.start()
        return self._snapshot()

    async def refresh(self) -> SetupStatus:
        if self.tool_platform is None or not self.tool_platform.execution_enabled:
            return self._snapshot()
        if self._preparation_running():
            return self._snapshot()
        task = self._refresh_task
        if task is not None and not task.done():
            await task
        else:
            await self._refresh()
        return self._snapshot()

    async def _refresh(self) -> None:
        async with self._refresh_lock:
            self._set_terminal(
                TerminalSetupStatus(
                    status=TerminalSetupState.DETECTING_RUNNER,
                    detail="Checking supported local container runtimes.",
                )
            )
            self._emit(SetupEventReason.RUNTIME_DETECTION_STARTED)
            try:
                profiles = self.store.list_entities(StoredRunnerProfile, limit=1_000)
                if profiles:
                    candidates = await asyncio.gather(
                        *(self._verify_configured(profile) for profile in profiles)
                    )
                    self._set_terminal(self._resolve_candidates(list(candidates)))
                    self._emit(SetupEventReason.RUNTIME_DETECTION_COMPLETED)
                    return

                candidates = await self._detect_fixed_candidates()
                healthy = [candidate for candidate in candidates if candidate.healthy]
                if len(healthy) == 1:
                    selected = healthy[0]
                    profile = self._stored_profile(selected)
                    try:
                        profile = self.store.create(profile)
                    except ConflictError as caught_error:
                        record_caught_exception(
                            "setup",
                            "setup.setup.caught_failure_002",
                            "A handled setup operation raised an exception.",
                            caught_error,
                            stage="setup",
                        )
                        existing = self.store.get(StoredRunnerProfile, "local")
                        if not self._same_runner(existing, profile):
                            self._set_terminal(
                                TerminalSetupStatus(
                                    status=TerminalSetupState.NEEDS_RUNNER,
                                    candidates=candidates,
                                    detail=(
                                        "A different local runner profile already "
                                        "exists. Review it in Advanced setup."
                                    ),
                                )
                            )
                            self._emit(SetupEventReason.RUNTIME_DETECTION_COMPLETED)
                            return
                        profile = existing
                    selected = selected.model_copy(
                        update={"runner_profile_id": profile.id}
                    )
                    candidates = [
                        selected if item.executable == selected.executable else item
                        for item in candidates
                    ]
                    self._set_terminal(
                        TerminalSetupStatus(
                            status=self._selected_runner_terminal_state(),
                            runner_profile_id=profile.id,
                            candidates=candidates,
                            detail=self._selected_runner_detail(selected.detail),
                        )
                    )
                elif len(healthy) > 1:
                    self._set_terminal(
                        TerminalSetupStatus(
                            status=TerminalSetupState.NEEDS_RUNNER,
                            candidates=candidates,
                            detail=(
                                "Choose one of the verified local container runtimes."
                            ),
                        )
                    )
                else:
                    self._set_terminal(
                        TerminalSetupStatus(
                            status=TerminalSetupState.NEEDS_RUNNER,
                            candidates=candidates,
                            detail=(
                                "Install or start a supported rootless Docker or "
                                "Podman runtime. Host-shell fallback is not permitted."
                            ),
                        )
                    )
                self._emit(SetupEventReason.RUNTIME_DETECTION_COMPLETED)
            except asyncio.CancelledError as caught_error:
                record_caught_exception(
                    "setup",
                    "setup.setup.caught_failure_003",
                    "A handled setup operation raised an exception.",
                    caught_error,
                    stage="setup",
                )
                raise
            except Exception as caught_error:
                record_caught_exception(
                    "setup",
                    "setup.setup.caught_failure_004",
                    "A handled setup operation raised an exception.",
                    caught_error,
                    stage="setup",
                )
                self._set_terminal(
                    TerminalSetupStatus(
                        status=TerminalSetupState.ERROR,
                        detail="Local container runtime detection failed. Retry setup.",
                    )
                )
                self._emit(SetupEventReason.RUNTIME_DETECTION_COMPLETED)

    async def select_runner(
        self, request: RunnerSelectionRequest
    ) -> SetupControlResponse:
        """Persist one currently detected fixed-path candidate as ``local``."""

        if self.tool_platform is None or not self.tool_platform.execution_enabled:
            raise SetupServiceError(
                "terminal_disabled",
                "container terminal execution is disabled in this Core",
                status_code=503,
            )
        async with self._refresh_lock:
            candidate = next(
                (
                    item
                    for item in self._terminal.candidates
                    if item.candidate_id == request.candidate_id
                ),
                None,
            )
            if candidate is None:
                raise SetupServiceError(
                    "runner_candidate_not_found",
                    "the runner candidate is no longer available; refresh setup",
                    status_code=404,
                )
            if (
                candidate.source == RunnerCandidateSource.CONFIGURED
                and candidate.runner_profile_id == "local"
                and candidate.healthy
            ):
                return SetupControlResponse(
                    operation="runner_selection",
                    accepted=True,
                    idempotent=True,
                    setup=self._snapshot(),
                )
            if candidate.source != RunnerCandidateSource.DETECTED:
                raise SetupServiceError(
                    "runner_candidate_not_selectable",
                    "only a verified fixed-path detected candidate can be selected",
                )
            if not candidate.healthy:
                raise SetupServiceError(
                    "runner_candidate_unhealthy",
                    "the selected runner candidate is not healthy",
                )

            trusted_paths = {
                str(path): path
                for path in ContainerSandboxRunner.trusted_runtime_paths()
            }
            trusted_path = trusted_paths.get(candidate.executable)
            if (
                trusted_path is None
                or not trusted_path.is_file()
                or not os.access(trusted_path, os.X_OK)
            ):
                raise SetupServiceError(
                    "runner_candidate_not_found",
                    "the runner candidate is no longer available; refresh setup",
                    status_code=404,
                )
            verified = await self._inspect_path(trusted_path)
            if verified.candidate_id != request.candidate_id or not verified.healthy:
                raise SetupServiceError(
                    "runner_candidate_unhealthy",
                    "the selected runner candidate did not pass re-verification",
                )
            profile = self._stored_profile(verified)
            idempotent = False
            try:
                profile = self.store.create(profile)
            except ConflictError as caught_error:
                record_caught_exception(
                    "setup",
                    "setup.setup.caught_failure_005",
                    "A handled setup operation raised an exception.",
                    caught_error,
                    stage="setup",
                )
                existing = self.store.get(StoredRunnerProfile, "local")
                if not self._same_runner(existing, profile):
                    raise SetupServiceError(
                        "local_runner_conflict",
                        "a different local runner profile already exists; refresh setup",
                    ) from None
                profile = existing
                idempotent = True

            selected = verified.model_copy(update={"runner_profile_id": profile.id})
            candidates = [
                selected if item.candidate_id == selected.candidate_id else item
                for item in self._terminal.candidates
            ]
            self._set_terminal(
                TerminalSetupStatus(
                    status=self._selected_runner_terminal_state(),
                    runner_profile_id=profile.id,
                    candidates=candidates,
                    detail=self._selected_runner_detail(selected.detail),
                )
            )
            self._emit(SetupEventReason.RUNNER_SELECTED)
            return SetupControlResponse(
                operation="runner_selection",
                accepted=True,
                idempotent=idempotent,
                setup=self._snapshot(),
            )

    async def prepare_image(
        self, request: ImagePreparationRequest
    ) -> SetupControlResponse:
        return await self._start_image_preparation(request, retry=False)

    async def retry_image_preparation(
        self, request: ImagePreparationRequest
    ) -> SetupControlResponse:
        return await self._start_image_preparation(request, retry=True)

    async def cancel_image_preparation(
        self, request: ImagePreparationCancellationRequest
    ) -> SetupControlResponse:
        async with self._preparation_lock:
            current = self._image_preparation
            if current.operation_id != request.operation_id:
                raise SetupServiceError(
                    "image_preparation_not_found",
                    "the image-preparation operation does not exist",
                    status_code=404,
                )
            if current.phase in {
                ImagePreparationPhase.CANCELLED,
                ImagePreparationPhase.ERROR,
                ImagePreparationPhase.READY,
            }:
                return SetupControlResponse(
                    operation="image_preparation_cancellation",
                    accepted=False,
                    idempotent=True,
                    operation_id=current.operation_id,
                    setup=self._snapshot(),
                )
            task = self._preparation_task
            self._transition_preparation(
                current.model_copy(
                    update={
                        "phase": ImagePreparationPhase.CANCELLING,
                        "can_cancel": False,
                        "detail": "Cancelling workstation image preparation.",
                    }
                ),
                terminal_state=TerminalSetupState.PREPARING_IMAGE,
            )
            self._emit(SetupEventReason.IMAGE_PREPARATION_CANCELLING)
            if task is not None and not task.done():
                task.cancel()

        if task is not None:
            await gather_diagnostic(
                task,
                feature="setup",
                event_code="setup.image_preparation.cancellation_failed",
                failure_message="Image preparation did not cancel cleanly.",
                stage="cancellation",
            )
        if self._image_preparation.phase == ImagePreparationPhase.CANCELLING:
            self._mark_preparation_cancelled(request.operation_id)
        return SetupControlResponse(
            operation="image_preparation_cancellation",
            accepted=True,
            idempotent=False,
            operation_id=request.operation_id,
            setup=self._snapshot(),
        )

    def replay_events(self, after_sequence: int = 0) -> SetupEventReplay:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        latest = self._next_event_sequence - 1
        oldest = self._events[0].sequence if self._events else self._next_event_sequence
        return SetupEventReplay(
            events=tuple(
                event for event in self._events if event.sequence > after_sequence
            ),
            oldest_sequence=oldest,
            latest_sequence=latest,
            truncated=bool(self._events and after_sequence < oldest - 1),
        )

    async def events(
        self,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        keepalive_seconds: float = 15.0,
    ) -> AsyncIterator[SetupEvent | None]:
        """Replay retained snapshots, then yield live events and keepalives."""

        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        if keepalive_seconds <= 0:
            raise ValueError("keepalive_seconds must be positive")
        queue: asyncio.Queue[SetupEvent] = asyncio.Queue(maxsize=self._event_retention)
        self._event_subscribers.add(queue)
        cursor = after_sequence
        try:
            replay = self.replay_events(cursor)
            for event in replay.events:
                cursor = event.sequence
                yield event
            if not follow:
                return
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=keepalive_seconds
                    )
                except asyncio.TimeoutError as caught_error:
                    record_caught_exception(
                        "setup",
                        "setup.setup.caught_failure_006",
                        "A handled setup operation raised an exception.",
                        caught_error,
                        stage="setup",
                    )
                    yield None
                    continue
                if event.sequence <= cursor:
                    continue
                cursor = event.sequence
                yield event
        finally:
            self._event_subscribers.discard(queue)

    async def _start_image_preparation(
        self, request: ImagePreparationRequest, *, retry: bool
    ) -> SetupControlResponse:
        if self.tool_platform is None or not self.tool_platform.execution_enabled:
            raise SetupServiceError(
                "terminal_disabled",
                "container terminal execution is disabled in this Core",
                status_code=503,
            )
        requested_project_id = request.project_id
        if retry and requested_project_id is None:
            requested_project_id = self._image_preparation.project_id
        project_id = self._preparation_project_id(requested_project_id)
        try:
            profile = self.tool_platform.resolve_human_terminal_profile(project_id)
        except (NotFoundError, RuntimePlatformError) as exc:
            record_caught_exception(
                "setup",
                "setup.setup.caught_failure_007",
                "A handled setup operation raised an exception.",
                exc,
                stage="setup",
            )
            raise SetupServiceError(
                "runner_unavailable", str(exc), status_code=409
            ) from exc

        operation: Literal["image_preparation", "image_preparation_retry"] = (
            "image_preparation_retry" if retry else "image_preparation"
        )
        async with self._preparation_lock:
            current = self._image_preparation
            if self._preparation_running():
                if current.project_id != project_id:
                    raise SetupServiceError(
                        "image_preparation_busy",
                        "another Project image preparation is already running",
                    )
                return SetupControlResponse(
                    operation=operation,
                    accepted=False,
                    idempotent=True,
                    operation_id=current.operation_id,
                    setup=self._snapshot(),
                )
            if (
                current.phase == ImagePreparationPhase.READY
                and current.project_id == project_id
            ):
                return SetupControlResponse(
                    operation=operation,
                    accepted=False,
                    idempotent=True,
                    operation_id=current.operation_id,
                    setup=self._snapshot(),
                )
            if retry and current.phase not in {
                ImagePreparationPhase.ERROR,
                ImagePreparationPhase.CANCELLED,
                ImagePreparationPhase.NOT_STARTED,
            }:
                raise SetupServiceError(
                    "image_preparation_not_retryable",
                    "the current image-preparation operation cannot be retried",
                )

            operation_id = str(uuid4())
            preparation = ImagePreparationStatus(
                phase=ImagePreparationPhase.QUEUED,
                operation_id=operation_id,
                project_id=project_id,
                progress_indeterminate=True,
                can_cancel=True,
                detail="Workstation image preparation is queued.",
                started_at=utc_now(),
            )
            self._terminal = self._terminal.model_copy(
                update={"runner_profile_id": profile.id}
            )
            self._transition_preparation(
                preparation,
                terminal_state=TerminalSetupState.PREPARING_IMAGE,
            )
            self._emit(SetupEventReason.IMAGE_PREPARATION_QUEUED)
            self._preparation_task = create_diagnostic_task(
                self._run_image_preparation(operation_id, project_id),
                feature="setup",
                event_code="setup.image_preparation",
                failure_message="Workstation image preparation stopped unexpectedly.",
                name=f"nebula-image-preparation-{operation_id}",
            )
            return SetupControlResponse(
                operation=operation,
                accepted=True,
                idempotent=False,
                operation_id=operation_id,
                setup=self._snapshot(),
            )

    async def _run_image_preparation(self, operation_id: str, project_id: str) -> None:
        assert self.tool_platform is not None
        try:
            current = self._image_preparation
            self._transition_preparation(
                current.model_copy(
                    update={
                        "phase": ImagePreparationPhase.RESOLVING_RUNTIME,
                        "detail": "Verifying the selected local container runtime.",
                    }
                ),
                terminal_state=TerminalSetupState.PREPARING_IMAGE,
            )
            self._emit(SetupEventReason.IMAGE_PREPARATION_PROGRESS)
            await asyncio.sleep(0)
            current = self._image_preparation
            self._transition_preparation(
                current.model_copy(
                    update={
                        "phase": ImagePreparationPhase.PREPARING_IMAGE,
                        "detail": (
                            "Pulling or reusing the workstation image and verifying "
                            "its prepared runtime metadata."
                        ),
                    }
                ),
                terminal_state=TerminalSetupState.PREPARING_IMAGE,
            )
            self._emit(SetupEventReason.IMAGE_PREPARATION_PROGRESS)

            async def report_progress(detail: str) -> None:
                if self._image_preparation.operation_id != operation_id:
                    return
                progressing = self._image_preparation.model_copy(
                    update={
                        "phase": ImagePreparationPhase.PREPARING_IMAGE,
                        "progress_percent": None,
                        "progress_indeterminate": True,
                        "detail": detail,
                    }
                )
                self._transition_preparation(
                    progressing,
                    terminal_state=TerminalSetupState.PREPARING_IMAGE,
                )
                self._emit(SetupEventReason.IMAGE_PREPARATION_PROGRESS)

            resolution = await self.tool_platform.resolve_human_terminal_runtime(
                project_id,
                on_progress=report_progress,
            )
            image = resolution.image
            ready = self._image_preparation.model_copy(
                update={
                    "phase": ImagePreparationPhase.READY,
                    "progress_percent": 100,
                    "progress_indeterminate": False,
                    "can_cancel": False,
                    "can_retry": False,
                    "image_digest": image.digest,
                    "completed_at": utc_now(),
                    "detail": image.detail,
                }
            )
            self._transition_preparation(ready, terminal_state=TerminalSetupState.READY)
            self._emit(SetupEventReason.IMAGE_PREPARATION_READY)
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "setup",
                "setup.setup.caught_failure_008",
                "A handled setup operation raised an exception.",
                caught_error,
                stage="setup",
            )
            self._mark_preparation_cancelled(operation_id)
            raise
        except Exception as exc:
            record_caught_exception(
                "setup",
                "setup.setup.caught_failure_009",
                "A handled setup operation raised an exception.",
                exc,
                stage="setup",
            )
            if self._image_preparation.operation_id != operation_id:
                return
            failed = self._image_preparation.model_copy(
                update={
                    "phase": ImagePreparationPhase.ERROR,
                    "progress_indeterminate": False,
                    "can_cancel": False,
                    "can_retry": True,
                    "completed_at": utc_now(),
                    "detail": str(exc)[:2_000]
                    or "Workstation image preparation failed.",
                }
            )
            self._transition_preparation(
                failed, terminal_state=TerminalSetupState.ERROR
            )
            self._emit(SetupEventReason.IMAGE_PREPARATION_ERROR)

    def _mark_preparation_cancelled(self, operation_id: str) -> None:
        if self._image_preparation.operation_id != operation_id:
            return
        cancelled = self._image_preparation.model_copy(
            update={
                "phase": ImagePreparationPhase.CANCELLED,
                "progress_indeterminate": False,
                "can_cancel": False,
                "can_retry": True,
                "completed_at": utc_now(),
                "detail": "Workstation image preparation was cancelled.",
            }
        )
        terminal_state = (
            TerminalSetupState.PREPARING_IMAGE
            if self._terminal.runner_profile_id is not None
            else TerminalSetupState.NEEDS_RUNNER
        )
        self._transition_preparation(cancelled, terminal_state=terminal_state)
        self._emit(SetupEventReason.IMAGE_PREPARATION_CANCELLED)

    def _preparation_project_id(self, requested: str | None) -> str:
        if requested is not None:
            try:
                return self.store.get(Engagement, requested).id
            except NotFoundError as exc:
                record_caught_exception(
                    "setup",
                    "setup.setup.caught_failure_010",
                    "A handled setup operation raised an exception.",
                    exc,
                    stage="setup",
                )
                raise SetupServiceError(
                    "project_not_found",
                    "the requested Project does not exist",
                    status_code=404,
                ) from exc
        scratch = current_scratch_project_id(self.store)
        if scratch is not None:
            return scratch
        projects = self.store.list_entities(Engagement, limit=2)
        if len(projects) == 1:
            return projects[0].id
        raise SetupServiceError(
            "project_required",
            "select a Project before preparing the workstation image",
        )

    def _preparation_running(self) -> bool:
        task = self._preparation_task
        return task is not None and not task.done()

    def _transition_preparation(
        self,
        preparation: ImagePreparationStatus,
        *,
        terminal_state: TerminalSetupState | str,
    ) -> None:
        self._image_preparation = preparation
        self._terminal = self._terminal.model_copy(
            update={
                "status": terminal_state,
                "image_preparation": preparation,
                "detail": preparation.detail,
            }
        )

    def _set_terminal(self, terminal: TerminalSetupStatus) -> None:
        self._terminal = terminal.model_copy(
            update={"image_preparation": self._image_preparation}
        )

    def _emit(self, reason: SetupEventReason | str) -> SetupEvent:
        event = SetupEvent(
            sequence=self._next_event_sequence,
            occurred_at=utc_now(),
            reason=cast(SetupEventReason, reason),
            snapshot=self._snapshot(),
        )
        self._next_event_sequence += 1
        self._events.append(event)
        for queue in tuple(self._event_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty as caught_error:
                    record_caught_exception(
                        "setup",
                        "setup.setup.caught_failure_011",
                        "A handled setup operation raised an exception.",
                        caught_error,
                        stage="setup",
                    )
                    pass
            queue.put_nowait(event)
        return event

    @staticmethod
    def _same_runner(
        existing: StoredRunnerProfile, expected: StoredRunnerProfile
    ) -> bool:
        return (
            existing.runtime == expected.runtime
            and existing.executable == expected.executable
            and existing.context == expected.context
            and existing.platform == expected.platform
            and existing.isolation == expected.isolation
        )

    async def _verify_configured(self, profile: StoredRunnerProfile) -> RunnerCandidate:
        refreshed = profile
        detail = profile.last_health_detail
        if profile.enabled and self.tool_platform is not None:
            try:
                refreshed = await self.tool_platform.verify_runner(profile.id)
                detail = refreshed.last_health_detail
            except Exception as exc:
                record_caught_exception(
                    "setup",
                    "setup.setup.caught_failure_012",
                    "A handled setup operation raised an exception.",
                    exc,
                    stage="setup",
                )
                detail = str(exc)[:1_000]
                try:
                    refreshed = self.store.update(
                        StoredRunnerProfile,
                        profile.id,
                        {
                            "healthy": False,
                            "last_health_at": utc_now(),
                            "last_health_detail": detail,
                        },
                        expected_revision=profile.revision,
                    )
                except (ConflictError, NotFoundError) as caught_error:
                    record_caught_exception(
                        "setup",
                        "setup.setup.caught_failure_013",
                        "A handled setup operation raised an exception.",
                        caught_error,
                        stage="setup",
                    )
                    refreshed = self.store.get(StoredRunnerProfile, profile.id)
        return RunnerCandidate(
            candidate_id=self._candidate_id_for_executable(refreshed.executable),
            runner_profile_id=refreshed.id,
            source=RunnerCandidateSource.CONFIGURED,
            name=refreshed.name,
            runtime=refreshed.runtime,
            executable=refreshed.executable,
            context=refreshed.context,
            platform=cast(Literal["linux/amd64", "linux/arm64"], refreshed.platform),
            isolation=refreshed.isolation,
            healthy=bool(refreshed.enabled and refreshed.healthy),
            detail=detail,
        )

    async def _detect_fixed_candidates(self) -> list[RunnerCandidate]:
        paths = [
            path
            for path in ContainerSandboxRunner.trusted_runtime_paths()
            if path.is_file() and os.access(path, os.X_OK)
        ]
        return list(await asyncio.gather(*(self._inspect_path(path) for path in paths)))

    async def _inspect_path(self, path: Path) -> RunnerCandidate:
        profile = SandboxRunnerProfile.from_runtime(path)
        runner = ContainerSandboxRunner(profile=profile)
        healthy, detail = await runner.available()
        isolation = {
            RunnerIsolationMode.LINUX_ROOTLESS: RunnerIsolation.ROOTLESS,
            RunnerIsolationMode.PODMAN_MACHINE: RunnerIsolation.PODMAN_MACHINE,
            RunnerIsolationMode.DOCKER_DESKTOP_VM: RunnerIsolation.DOCKER_DESKTOP_VM,
        }[profile.isolation_mode]
        runtime = RunnerRuntime(profile.runtime_type.value)
        return RunnerCandidate(
            candidate_id=self._candidate_id_for_executable(str(profile.executable)),
            source=RunnerCandidateSource.DETECTED,
            name=f"Local {runtime.value.title()}",
            runtime=runtime,
            executable=str(profile.executable),
            context=profile.context,
            platform=_container_platform(),
            isolation=isolation,
            healthy=healthy,
            detail=detail,
        )

    @staticmethod
    def _candidate_id_for_executable(executable: str) -> str | None:
        trusted = {str(path) for path in ContainerSandboxRunner.trusted_runtime_paths()}
        if executable not in trusted:
            return None
        digest = hashlib.sha256(executable.encode("utf-8")).hexdigest()[:32]
        return f"fixed:{digest}"

    @staticmethod
    def _stored_profile(candidate: RunnerCandidate) -> StoredRunnerProfile:
        return StoredRunnerProfile(
            id="local",
            name=candidate.name,
            runtime=candidate.runtime,
            executable=candidate.executable,
            context=candidate.context,
            platform=candidate.platform,
            isolation=candidate.isolation,
            enabled=True,
            healthy=True,
            last_health_at=utc_now(),
            last_health_detail=candidate.detail,
        )

    def _resolve_candidates(
        self, candidates: list[RunnerCandidate]
    ) -> TerminalSetupStatus:
        healthy = [candidate for candidate in candidates if candidate.healthy]
        local = next(
            (
                candidate
                for candidate in healthy
                if candidate.runner_profile_id == "local"
            ),
            None,
        )
        selected = local or (healthy[0] if len(healthy) == 1 else None)
        if selected is not None:
            return TerminalSetupStatus(
                status=self._selected_runner_terminal_state(),
                runner_profile_id=selected.runner_profile_id,
                candidates=candidates,
                detail=self._selected_runner_detail(selected.detail),
            )
        if len(healthy) > 1:
            detail = "Choose a preferred runner profile named 'local'."
        elif candidates:
            detail = "Configured container runtimes are not currently healthy."
        else:
            detail = "No container runner is configured."
        return TerminalSetupStatus(
            status=TerminalSetupState.NEEDS_RUNNER,
            candidates=candidates,
            detail=detail,
        )

    def _selected_runner_terminal_state(self) -> TerminalSetupState:
        """Report readiness only after the workstation image is verified."""

        if self._image_preparation.phase == ImagePreparationPhase.READY:
            return TerminalSetupState.READY
        if self._image_preparation.phase == ImagePreparationPhase.ERROR:
            return TerminalSetupState.ERROR
        return TerminalSetupState.PREPARING_IMAGE

    def _selected_runner_detail(self, runner_detail: str | None) -> str:
        if self._image_preparation.phase == ImagePreparationPhase.READY:
            return (
                self._image_preparation.detail
                or runner_detail
                or ("Verified workstation image is ready.")
            )
        if self._image_preparation.detail:
            return self._image_preparation.detail
        return "A verified local runtime is ready; preparing the workstation image."

    def _snapshot(self) -> SetupStatus:
        providers = [
            provider
            for provider in self.store.list_entities(ProviderProfile, limit=1_000)
            if provider.enabled
        ]
        provider = providers[0] if providers else None
        assistant = (
            AssistantSetupStatus(
                status=AssistantSetupState.CONFIGURED,
                provider_profile_id=provider.id,
                detail="Model profile configured; connectivity is checked separately.",
            )
            if provider is not None
            else AssistantSetupStatus(
                status=AssistantSetupState.NEEDS_MODEL,
                detail="Assistant setup is optional; Terminal is available independently.",
            )
        )
        return SetupStatus(
            core=CoreSetupStatus(status=CoreSetupState.READY),
            scratch_project_id=current_scratch_project_id(self.store),
            terminal=self._terminal,
            assistant=assistant,
        )


def _container_platform() -> Literal["linux/amd64", "linux/arm64"]:
    machine = host_platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "linux/arm64"
    if machine in {"amd64", "x86_64"}:
        return "linux/amd64"
    raise ValueError("unsupported container architecture")


__all__ = [
    "AssistantSetupState",
    "AssistantSetupStatus",
    "CoreSetupState",
    "CoreSetupStatus",
    "DEFAULT_SETUP_EVENT_RETENTION",
    "ImagePreparationCancellationRequest",
    "ImagePreparationPhase",
    "ImagePreparationRequest",
    "ImagePreparationStatus",
    "MAX_SETUP_EVENT_RETENTION",
    "RunnerCandidate",
    "RunnerCandidateSource",
    "RunnerSelectionRequest",
    "SCRATCH_PROJECT_ID",
    "SCRATCH_PROJECT_NAME",
    "SetupControlResponse",
    "SetupEvent",
    "SetupEventReason",
    "SetupEventReplay",
    "SetupService",
    "SetupServiceError",
    "SetupStatus",
    "TerminalSetupState",
    "TerminalSetupStatus",
    "bootstrap_scratch_project",
    "current_scratch_project_id",
]
