"""Shadow curation of completed goals, inside the project execution worker.

The worker does only I/O here: it reads a completed goal's turns in bounded
chunks, makes the curation model call, and commits each chunk's cards, links
and run position in one short transaction. Prompt assembly, verification and
ranking run in the curation helper (``linked_memory_helper``).

Live work goes first. Before every chunk the curator pauses if the project had
a live turn within the quiet period, host pressure is above the curation
threshold, or the project is handing off. A provider call is admitted only
while no direct turn is using the provider, and at most one curation call runs
on the host. Nothing here runs on a turn's critical path, and nothing is
injected into any turn: this is stage 1, shadow curation.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .context import resolve_context_limits
from .diagnostics import (
    create_diagnostic_task,
    record_caught_exception,
    record_diagnostic,
)
from .domain import (
    ChatBackend,
    ChatGoal,
    ChatGoalStatus,
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    Engagement,
    ProviderProfile,
)
from .linked_memory import GoalRun, LinkedMemoryStore
from .linked_memory_helper import CurationHelper, HelperUnavailable
from .linked_memory_rules import project_stream_id
from .privacy import ProviderPrivacyViolation, validate_engagement_provider_privacy
from .provider_scheduler import CurationDeferred
from .providers import (
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ProviderContextLengthError,
    ProviderError,
    ProviderOverloadedError,
    ProviderQuotaError,
)
from .storage import ConflictError, NebulaStore, NotFoundError

MAX_GOAL_SESSIONS = 33  # the goal's own chat and up to 32 child chats


@dataclass(frozen=True)
class CuratorConfig:
    quiet_seconds: float = 30.0
    start_pressure: float = 0.50
    pause_pressure: float = 0.65
    poll_seconds: float = 5.0
    chunk_messages: int = 40
    max_chunk_chars: int = 24_000
    max_output_tokens: int = 1_200
    max_chunks_per_goal: int = 200
    max_tokens_per_goal: int = 400_000
    call_timeout: float = 180.0
    rate_limit_cooldown: float = 300.0
    max_chunk_failures: int = 3
    candidate_cards: int = 200

    @classmethod
    def from_environment(cls) -> "CuratorConfig":
        def number(name: str, default: float, low: float, high: float) -> float:
            raw = os.environ.get(name)
            if raw is None:
                return default
            try:
                value = float(raw)
            except ValueError:
                # diagnostic-expected: an invalid override keeps the documented default.
                return default
            return min(high, max(low, value))

        base = cls()
        return cls(
            quiet_seconds=number(
                "NEBULA_LINKED_MEMORY_QUIET_SECONDS", base.quiet_seconds, 0, 3600
            ),
            start_pressure=number(
                "NEBULA_LINKED_MEMORY_START_PRESSURE", base.start_pressure, 0.05, 1
            ),
            pause_pressure=number(
                "NEBULA_LINKED_MEMORY_PAUSE_PRESSURE", base.pause_pressure, 0.05, 1
            ),
            poll_seconds=number(
                "NEBULA_LINKED_MEMORY_POLL_SECONDS", base.poll_seconds, 0.05, 60
            ),
        )


@dataclass
class _ProjectState:
    task: asyncio.Task[Any] | None = None
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    paused_reason: str | None = None
    cooldown_until: float = 0.0
    chunk_failures: int = 0


class HostCurationLock:
    """At most one curation call on the host, across every worker process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # diagnostic-expected: another worker holds the host's curation slot; this run waits.
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(OSError):
                # diagnostic-expected: closing the file releases the lock even if unlock fails.
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _session_kind(session: ChatSession) -> str:
    metadata = session.metadata or {}
    if metadata.get("subagent_id"):
        return "subagent"
    if metadata.get("delegated_from_goal_id"):
        return "goal_child"
    return "main"


def _step_text(step: dict[str, Any]) -> str:
    summary = step.get("result_summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    result = step.get("provider_result")
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)[:4_000]
    except (TypeError, ValueError):
        # diagnostic-expected: an unserializable tool result is left out of curation input.
        return ""


class LinkedMemoryCurator:
    """Consume ``goal.completed`` events and curate their goals in chunks."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        provider_factory: Callable[[ProviderProfile], ModelProvider],
        data_root: Path,
        chat: Any = None,
        scheduler: Any = None,
        host_load: Any = None,
        handoff_active: Callable[[], Awaitable[bool]] | None = None,
        helper: CurationHelper | None = None,
        config: CuratorConfig | None = None,
    ) -> None:
        self.store = store
        self.memory = LinkedMemoryStore(store)
        self.provider_factory = provider_factory
        self.chat = chat
        self.scheduler = scheduler
        self.host_load = host_load
        self.handoff_active = handoff_active
        self.helper = helper or CurationHelper()
        self.config = config or CuratorConfig.from_environment()
        self.host_lock = HostCurationLock(data_root / "linked-memory" / "curation.lock")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._projects: dict[str, _ProjectState] = {}
        self._last_live = 0.0
        self._registered = False
        self.chunks_curated = 0

    # Lifecycle -----------------------------------------------------------------

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not self._registered:
            self.store.on_project_oversight_change(self.notify)
            self.store.on_chat_activity_change(self.mark_live)
            self._registered = True
        self._last_live = time.monotonic()
        try:
            projects = await self.memory.enabled_projects()
        except Exception as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.startup_scan_failed",
                "Linked memory could not list enabled projects at startup.",
                error,
                stage="startup",
            )
            return
        for project_id in projects:
            self._wake(project_id)

    async def shutdown(self) -> None:
        self._loop = None
        tasks = [state.task for state in self._projects.values() if state.task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                # diagnostic-expected: create_diagnostic_task already recorded any loop failure.
                await task
        self._projects.clear()
        await self.helper.stop()

    def mark_live(self) -> None:
        """A chat record changed in this process: restart the quiet period."""

        self._last_live = time.monotonic()

    def notify(self, project_id: str) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._wake, project_id)

    def wake(self, project_id: str) -> None:
        """Start or wake the project's curation loop from the event loop."""

        if self._loop is not None:
            self._wake(project_id)

    def _wake(self, project_id: str) -> None:
        state = self._projects.setdefault(project_id, _ProjectState())
        state.signal.set()
        if state.task is None or state.task.done():
            state.task = create_diagnostic_task(
                self._run_project(project_id),
                feature="linked_memory",
                event_code="linked_memory.project_loop_failed",
                failure_message="Linked memory curation stopped unexpectedly.",
                name=f"nebula-linked-memory-{project_id[:32]}",
            )

    def snapshot(self, project_id: str) -> dict[str, Any]:
        state = self._projects.get(project_id)
        running = state is not None and state.task is not None and not state.task.done()
        return {
            "curator": "paused"
            if state and state.paused_reason
            else ("running" if running else "idle"),
            "paused_reason": state.paused_reason if state else None,
            "helper": self.helper.snapshot(),
        }

    # Project loop ----------------------------------------------------------------

    async def _run_project(self, project_id: str) -> None:
        state = self._projects.setdefault(project_id, _ProjectState())
        while self._loop is not None:
            state.signal.clear()
            if await self.memory.effective_stage(project_id) == "off":
                state.paused_reason = None
                return
            await self._consume_events(project_id)
            runs = await self.memory.open_runs(project_id)
            if not runs:
                state.paused_reason = None
                return
            stopped = False
            for run in runs:
                outcome = await self._curate(project_id, run, state)
                if outcome == "stop":
                    stopped = True
                    break
            if stopped:
                return

    async def _consume_events(self, project_id: str) -> None:
        cursor = await self.memory.cursor(project_id)
        if cursor is None:
            cursor = await self.memory.latest_event_sequence(project_id)
            await self.memory.advance_cursor(project_id, cursor)
            return
        for _page in range(10):
            events = await self.store.replay_operation_events(
                project_stream_id(project_id),
                after_sequence=cursor,
                event_types=["goal.completed"],
                limit=100,
            )
            if not events:
                return
            for event in events:
                await self._start_run(project_id, event.payload)
                await self.memory.advance_cursor(project_id, event.sequence)
                cursor = event.sequence

    async def _start_run(self, project_id: str, payload: dict[str, Any]) -> None:
        goal_id = str(payload.get("goal_id") or "")
        if not goal_id or payload.get("parent_goal_id"):
            return
        try:
            goal = await self.store.get(ChatGoal, goal_id)
            session = await self.store.get(ChatSession, goal.session_id)
        except NotFoundError:
            # diagnostic-expected: a goal deleted after it completed has nothing to curate.
            return
        if (
            goal.engagement_id != project_id
            or goal.parent_goal_id
            or goal.status != ChatGoalStatus.COMPLETED
            or _session_kind(session) != "main"
        ):
            return
        session_ids = [goal.session_id]
        for child in goal.child_session_ids:
            if child not in session_ids:
                session_ids.append(child)
        subagents = getattr(self.chat, "subagents", None)
        if subagents is not None:
            try:
                for record in await subagents.descendants_for_session(goal.session_id):
                    if (
                        record.child_session_id
                        and record.child_session_id not in session_ids
                    ):
                        session_ids.append(record.child_session_id)
            except ConflictError as error:
                record_caught_exception(
                    "linked_memory",
                    "linked_memory.subagent_chain_invalid",
                    "A goal's subagent chain could not be read; only its own chat is curated.",
                    error,
                    stage="goal_run",
                )
        run = await self.memory.create_run(
            project_id, goal_id, session_ids[:MAX_GOAL_SESSIONS]
        )
        record_diagnostic(
            "info",
            "linked_memory",
            "linked_memory.goal_run_created",
            "Linked memory will curate a completed goal.",
            project_id=project_id,
            goal_id=goal_id,
            sessions=len(run.session_ids),
        )

    # Gates -------------------------------------------------------------------------

    def _live_now(self) -> bool:
        chat = self.chat
        if chat is None:
            return False
        turns = getattr(chat, "_active_provider_turns", {}) or {}
        return any(not turn.done for turn in turns.values())

    async def _pressure(self) -> float | None:
        gate = self.host_load or getattr(self.scheduler, "host_load", None)
        if gate is None:
            return None
        try:
            snapshot = await gate.snapshot()
        except Exception as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.host_pressure_unavailable",
                "Host pressure could not be read; curation waits.",
                error,
                stage="gate",
            )
            return 1.0
        readings = [
            value for value in snapshot.readings().values() if value is not None
        ]
        return max(readings) if readings else 0.0

    async def _gate(self, state: _ProjectState, *, starting: bool) -> str | None:
        """Why curation must not run a chunk now, or None."""

        chat = self.chat
        if chat is not None and (
            getattr(chat, "shutting_down", False)
            or getattr(chat, "restoring_handoff", False)
        ):
            return "handoff"
        if self.handoff_active is not None and await self.handoff_active():
            return "handoff"
        if self._live_now():
            self._last_live = time.monotonic()
            return "live_turn"
        if time.monotonic() - self._last_live < self.config.quiet_seconds:
            return "quiet_period"
        if time.monotonic() < state.cooldown_until:
            return "provider_cooldown"
        pressure = await self._pressure()
        limit = self.config.start_pressure if starting else self.config.pause_pressure
        if pressure is not None and pressure > limit:
            return "host_pressure"
        return None

    async def _wait_for_gate(
        self, project_id: str, run: GoalRun, state: _ProjectState, *, starting: bool
    ) -> tuple[GoalRun, bool]:
        """Wait at a chunk boundary until the gates clear. False means stop."""

        while self._loop is not None:
            reason = await self._gate(state, starting=starting)
            if reason is None:
                state.paused_reason = None
                if run.state != "running":
                    run = await self._set_state(run, "running", reason=None)
                return run, True
            if reason == "handoff":
                state.paused_reason = reason
                return run, False
            if state.paused_reason != reason:
                state.paused_reason = reason
                run = await self._set_state(run, "paused", reason=reason)
            if await self.memory.effective_stage(project_id) == "off":
                return run, False
            with contextlib.suppress(TimeoutError):
                # diagnostic-expected: the poll interval elapsed; the gates are checked again.
                await asyncio.wait_for(
                    state.signal.wait(), timeout=self.config.poll_seconds
                )
            state.signal.clear()
        return run, False

    async def _set_state(self, run: GoalRun, value: str, **fields: Any) -> GoalRun:
        try:
            return await self.memory.set_run_state(run, value, **fields)
        except ConflictError:
            # diagnostic-expected: another writer moved the run; continue from its latest record.
            latest = await self.memory.run(run.goal_id)
            return latest or run

    # Curation -----------------------------------------------------------------------

    async def _curate(self, project_id: str, run: GoalRun, state: _ProjectState) -> str:
        sessions: dict[str, tuple[ChatSession, list[dict[str, Any]]]] = {}
        starting = True
        while self._loop is not None:
            run, proceed = await self._wait_for_gate(
                project_id, run, state, starting=starting
            )
            if not proceed:
                return "stop"
            starting = False
            if run.chunks >= self.config.max_chunks_per_goal or (
                run.input_tokens + run.output_tokens >= self.config.max_tokens_per_goal
            ):
                await self._set_state(
                    run, "capped", reason="per-goal spend cap reached"
                )
                return "next"
            chunk = await self._read_chunk(run, sessions)
            if chunk is None:
                await self._set_state(run, "done", reason=None)
                record_diagnostic(
                    "info",
                    "linked_memory",
                    "linked_memory.goal_run_done",
                    "Linked memory finished curating a goal.",
                    project_id=project_id,
                    goal_id=run.goal_id,
                    chunks=run.chunks,
                )
                return "next"
            session, entries, position = chunk
            skip = self._skip_reason(session)
            if skip is not None or not entries:
                run, _counts = await self.memory.commit_chunk(
                    run,
                    cards=[],
                    links=[],
                    position=position,
                    usage={},
                    model="",
                    reason=skip,
                )
                continue
            outcome, run = await self._curate_chunk(
                project_id, run, state, session, entries, position
            )
            if outcome == "stop":
                return "stop"
            if outcome == "failed":
                return "next"
        return "stop"

    def _skip_reason(self, session: ChatSession) -> str | None:
        if session.backend == ChatBackend.HARNESS:
            return "harness chats are curated through memory.propose in a later release"
        if session.backend != ChatBackend.PROVIDER or not session.provider_profile_id:
            return "chat has no provider model"
        return None

    async def _read_chunk(
        self,
        run: GoalRun,
        cache: dict[str, tuple[ChatSession, list[dict[str, Any]]]],
    ) -> tuple[ChatSession, list[dict[str, Any]], dict[str, Any]] | None:
        position = dict(run.position)
        index = int(position.get("session", 0))
        after = int(position.get("after", 0))
        while index < len(run.session_ids):
            session_id = run.session_ids[index]
            if session_id not in cache:
                cache.clear()
                try:
                    session = await self.store.get(ChatSession, session_id)
                except NotFoundError:
                    # diagnostic-expected: a deleted child chat is skipped.
                    index, after = index + 1, 0
                    continue
                cache[session_id] = (session, await self._session_entries(session))
            session, entries = cache[session_id]
            pending = [entry for entry in entries if entry["sequence"] > after]
            if not pending:
                index, after = index + 1, 0
                continue
            selected: list[dict[str, Any]] = []
            chars = 0
            messages = 0
            last_sequence = after
            for entry in pending:
                if entry["sequence"] != last_sequence:
                    if messages >= self.config.chunk_messages or (
                        chars >= self.config.max_chunk_chars and selected
                    ):
                        break
                    messages += 1
                    last_sequence = entry["sequence"]
                selected.append(entry)
                chars += len(entry["text"])
            # Never split one message's tool steps across chunks.
            selected = [
                entry for entry in selected if entry["sequence"] <= last_sequence
            ]
            return session, selected, {"session": index, "after": last_sequence}
        return None

    async def _session_entries(self, session: ChatSession) -> list[dict[str, Any]]:
        """Transcript entries of one chat, oldest first, with tool steps."""

        messages = await self.store.list_session_entities(ChatMessage, session.id)
        turns = await self.store.list_session_entities(ChatTurn, session.id)
        by_message = {
            turn.final_message_id: turn for turn in turns if turn.final_message_id
        }
        ledger = getattr(self.chat, "turn_ledger", None)
        entries: list[dict[str, Any]] = []
        for message in sorted(messages, key=lambda item: item.sequence):
            if (message.metadata or {}).get("retracted_at"):
                continue
            if message.role == ChatRole.SYSTEM:
                continue
            turn = by_message.get(message.id)
            if message.role == ChatRole.ASSISTANT and turn is not None and ledger:
                try:
                    steps = await ledger.history(turn)
                except Exception as error:
                    record_caught_exception(
                        "linked_memory",
                        "linked_memory.tool_steps_unreadable",
                        "A turn's tool steps could not be read for curation.",
                        error,
                        stage="read_chunk",
                    )
                    steps = []
                for step in steps:
                    name = step.get("tool_name") or step.get("name")
                    text = _step_text(step)
                    if not name or not text.strip():
                        continue
                    entries.append(
                        {
                            "sequence": message.sequence,
                            "role": "tool",
                            "tool_name": str(name)[:120],
                            "text": text[:4_000],
                            "ref": {
                                "session_id": session.id,
                                "turn_id": turn.id,
                                "tool_call_id": step.get("tool_call_id"),
                                "tool_name": str(name)[:120],
                            },
                        }
                    )
            text = message.content or ""
            if not text.strip():
                continue
            entries.append(
                {
                    "sequence": message.sequence,
                    "role": "operator" if message.role == ChatRole.USER else "agent",
                    "tool_name": None,
                    "text": text[:8_000],
                    "ref": {
                        "session_id": session.id,
                        "message_id": message.id,
                        "sequence": message.sequence,
                    },
                }
            )
        return entries

    async def _curate_chunk(
        self,
        project_id: str,
        run: GoalRun,
        state: _ProjectState,
        session: ChatSession,
        entries: list[dict[str, Any]],
        position: dict[str, Any],
    ) -> tuple[str, GoalRun]:
        assert session.provider_profile_id is not None
        try:
            profile = await self.store.get(ProviderProfile, session.provider_profile_id)
            project = await self.store.get(Engagement, project_id)
            goal = await self.store.get(ChatGoal, run.goal_id)
        except NotFoundError:
            # diagnostic-expected: the chat's provider was removed; its turns are skipped.
            run, _ = await self.memory.commit_chunk(
                run,
                cards=[],
                links=[],
                position=position,
                usage={},
                model="",
                reason="provider removed",
            )
            return "next_chunk", run
        model = session.model or str(profile.metadata.get("default_model") or "")
        if not profile.enabled or not model:
            run, _ = await self.memory.commit_chunk(
                run,
                cards=[],
                links=[],
                position=position,
                usage={},
                model="",
                reason="provider disabled",
            )
            return "next_chunk", run
        if not profile.is_local and not profile.privacy.permits_sensitive_data:
            run, _ = await self.memory.commit_chunk(
                run,
                cards=[],
                links=[],
                position=position,
                usage={},
                model="",
                reason="provider not authorized for background project data",
            )
            return "next_chunk", run
        provider = self.provider_factory(profile)
        try:
            await validate_engagement_provider_privacy(self.store, project, provider)
        except ProviderPrivacyViolation as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.provider_privacy",
                "The project's privacy policy forbids this chat's provider for curation.",
                error,
                stage="provider_call",
            )
            run, _ = await self.memory.commit_chunk(
                run,
                cards=[],
                links=[],
                position=position,
                usage={},
                model="",
                reason="project privacy policy forbids the provider",
            )
            return "next_chunk", run
        limits = resolve_context_limits(profile, model=model)
        output_tokens = min(
            self.config.max_output_tokens, max(256, limits.max_output_tokens)
        )
        budget_chars = max(
            2_000,
            min(
                self.config.max_chunk_chars,
                (limits.context_window - output_tokens - 900) * 3,
            ),
        )
        candidates = [
            card.candidate()
            for card in await self.memory.candidate_cards(
                project_id, self.config.candidate_cards
            )
        ]
        chunk = {
            "goal": {
                "id": goal.id,
                "objective": goal.objective,
                "completion_summary": goal.completion_summary,
            },
            "entries": entries,
        }
        try:
            prompt = await self.helper.call(
                "prepare",
                {
                    "chunk": chunk,
                    "candidates": candidates,
                    "budget_chars": budget_chars,
                },
            )
        except HelperUnavailable as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.helper_unavailable",
                "The curation helper is unavailable; the chunk waits.",
                error,
                stage="prepare",
            )
            state.cooldown_until = time.monotonic() + self.config.poll_seconds * 6
            return "wait", await self._set_state(
                run, "paused", reason="helper unavailable"
            )
        if not self.host_lock.try_acquire():
            state.cooldown_until = time.monotonic() + self.config.poll_seconds
            return "wait", run
        started = time.monotonic()
        try:
            response = await self._complete(
                provider, model, prompt, output_tokens, project_id
            )
        except CurationDeferred as deferred:
            # diagnostic-expected: live work holds the provider; the chunk waits at its boundary.
            state.cooldown_until = time.monotonic() + self.config.poll_seconds
            return "wait", await self._set_state(run, "paused", reason=deferred.reason)
        except (ProviderOverloadedError, ProviderQuotaError) as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.provider_rate_limited",
                "The provider refused a curation call; the goal run pauses.",
                error,
                stage="provider_call",
            )
            state.cooldown_until = time.monotonic() + self.config.rate_limit_cooldown
            return "wait", await self._set_state(
                run,
                "paused",
                reason="provider rate limited",
                last_error=type(error).__name__,
            )
        except (ProviderContextLengthError, ProviderError, TimeoutError) as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.provider_call_failed",
                "A curation model call failed.",
                error,
                stage="provider_call",
            )
            state.chunk_failures += 1
            if state.chunk_failures >= self.config.max_chunk_failures:
                state.chunk_failures = 0
                run, _ = await self.memory.commit_chunk(
                    run,
                    cards=[],
                    links=[],
                    position=position,
                    usage={},
                    model=model,
                    reason=f"chunk skipped after {self.config.max_chunk_failures} failed calls",
                )
                return "next_chunk", run
            state.cooldown_until = time.monotonic() + self.config.poll_seconds * 6
            return "wait", await self._set_state(
                run,
                "paused",
                reason="model call failed",
                last_error=type(error).__name__,
            )
        finally:
            self.host_lock.release()
        state.chunk_failures = 0
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": response.usage.cost_usd or 0.0,
        }
        try:
            verified = await self.helper.call(
                "verify",
                {
                    "chunk": chunk,
                    "prompt": {
                        "refs": prompt.get("refs") or {},
                        "candidates": prompt.get("candidates") or {},
                    },
                    "candidates": [
                        card
                        for card in candidates
                        if card["id"] in set(prompt.get("shown") or [])
                    ],
                    "text": response.text,
                },
            )
        except HelperUnavailable as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.helper_unavailable",
                "The curation helper is unavailable; the chunk waits.",
                error,
                stage="verify",
            )
            return "wait", await self._set_state(
                run, "paused", reason="helper unavailable"
            )
        reason = "model output was not JSON" if verified.get("error") else None
        try:
            run, counts = await self.memory.commit_chunk(
                run,
                cards=list(verified.get("cards") or []),
                links=list(verified.get("links") or []),
                position=position,
                usage=usage,
                model=f"{profile.id}/{response.model or model}",
                reason=reason,
            )
        except ConflictError as error:
            record_caught_exception(
                "linked_memory",
                "linked_memory.chunk_commit_conflict",
                "A curated chunk could not be saved; it runs again.",
                error,
                stage="commit",
            )
            latest = await self.memory.run(run.goal_id)
            return "wait", latest or run
        self.chunks_curated += 1
        record_diagnostic(
            "info",
            "linked_memory",
            "linked_memory.chunk_curated",
            "Linked memory curated one chunk of a completed goal.",
            project_id=project_id,
            goal_id=run.goal_id,
            entries=len(entries),
            proposals=int(verified.get("proposals") or 0),
            rejected=len(verified.get("rejected") or []),
            cards=counts["cards"],
            attached=counts["attached"],
            links=counts["links"],
            superseded=counts["superseded"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            call_ms=int((time.monotonic() - started) * 1000),
            embedding=prompt.get("embedding"),
        )
        return "next_chunk", run

    async def _complete(
        self,
        provider: ModelProvider,
        model: str,
        prompt: dict[str, Any],
        output_tokens: int,
        project_id: str,
    ):
        request = ModelRequest(
            model=model,
            instructions=str(prompt["instructions"]),
            messages=[ModelMessage(role="user", content=str(prompt["content"]))],
            max_output_tokens=output_tokens,
            temperature=0,
            reasoning_effort="none",
            metadata={
                "engagement_id": project_id,
                "operation": "linked_memory_curation",
                "prompt_version": "lm-1",
            },
        )
        provider.require(request)
        scheduler = self.scheduler
        if scheduler is None:
            return await asyncio.wait_for(
                provider.complete(request), timeout=self.config.call_timeout
            )
        async with scheduler.curation_call():
            return await asyncio.wait_for(
                provider.complete(request), timeout=self.config.call_timeout
            )
