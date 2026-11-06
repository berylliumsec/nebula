"""Revision-fenced runtime selection shared by messages and Core-owned goals."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from .chat_turn_headers import session_turn_headers
from .database import EntityRow
from .diagnostics import create_diagnostic_task
from .domain import ChatBackend, ChatSession, ProviderProfile
from .storage import ConflictError, NotFoundError


class RuntimeSwitchService:
    def __init__(self, chat) -> None:
        self.chat = chat
        self.store = chat.store
        self._pending: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self._stopping = False

    async def startup(self) -> None:
        self._stopping = False
        await self.reconcile()

    async def reconcile(self) -> None:
        for session in await self.store.find_entities(
            ChatSession, {"metadata.pending_runtime_switch.status": "waiting_for_idle"}
        ):
            self._pending.add(session.id)
            self.schedule({session.id})

    async def shutdown(self) -> None:
        self._stopping = True
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()))

    def schedule(self, session_ids) -> None:
        if self._stopping:
            return
        for session_id in set(session_ids) & self._pending:
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                self._dirty.add(session_id)
                continue
            task = create_diagnostic_task(
                self.settle(session_id),
                feature="chat",
                event_code="chat.runtime_switch_activation",
                failure_message="A pending runtime switch could not be activated.",
                name="nebula-runtime-switch",
            )
            self._tasks[session_id] = task

            def finished(_, selected=session_id):
                self._tasks.pop(selected, None)
                if selected in self._dirty:
                    self._dirty.discard(selected)
                    self.schedule({selected})

            task.add_done_callback(finished)

    @staticmethod
    async def busy_reason(database, session: ChatSession) -> str | None:
        """Read under the caller's transaction; a blocked goal alone is idle."""
        if any(
            header.status and RuntimeSwitchService._pending_header(header)
            for header in await session_turn_headers(database, session.id)
        ):
            return "Waiting for the current response, approval, or interrupted-turn recovery."
        goal_payloads = await database.scalars(
            select(EntityRow.payload).where(
                EntityRow.kind == "chat_goals", EntityRow.chat_session_id == session.id
            )
        )
        if any(payload.get("execution_claim_id") for payload in goal_payloads):
            return "Waiting for the current goal turn to release its claim."
        children = await database.scalars(
            select(EntityRow.payload).where(
                EntityRow.kind == "chat_subagents",
                EntityRow.engagement_id == session.engagement_id,
            )
        )
        descendants: dict[str, list[dict]] = {}
        for child in children:
            descendants.setdefault(child.get("parent_session_id"), []).append(child)
        pending, seen = [session.id], {session.id}
        while pending:
            for child in descendants.get(pending.pop(), []):
                if child.get("status") == "running":
                    return "Waiting for this conversation’s subagents to finish."
                child_id = child.get("child_session_id")
                if child_id in seen:
                    return "Review this conversation’s inconsistent subagent ancestry."
                if child_id:
                    seen.add(child_id)
                    pending.append(child_id)
        queues = await database.scalars(
            select(EntityRow.payload).where(
                EntityRow.kind == "chat_queues", EntityRow.chat_session_id == session.id
            )
        )
        if any(
            item.get("status") not in {"complete", "cancelled"}
            for queue in queues
            for item in queue.get("items", [])
        ):
            return "Waiting for saved follow-ups to finish or be removed."
        if session.backend == ChatBackend.HARNESS and session.harness_session_id:
            active = await database.scalar(
                select(EntityRow.id)
                .where(
                    EntityRow.kind == "harness_turns",
                    EntityRow.harness_session_id == session.harness_session_id,
                    EntityRow.payload["status"]
                    .as_string()
                    .in_(["queued", "running", "waiting_approval"]),
                )
                .limit(1)
            )
            if active:
                return "Waiting for the native harness turn to finish."
        return None

    @staticmethod
    def _pending_header(header) -> bool:
        # Keep one authoritative definition of recoverable/claimed turn state.
        from .chat import ChatService

        return ChatService._header_is_pending(header)

    async def reason(self, session: ChatSession) -> str | None:
        async with self.store.database.session() as database:
            return await self.busy_reason(database, session)

    @staticmethod
    def binding(settings: dict) -> dict:
        harness = settings["backend"] == "harness"
        return {
            "backend": ChatBackend.HARNESS if harness else ChatBackend.PROVIDER,
            "harness_profile_id": settings["profile_id"] if harness else None,
            "harness_session_id": None,
            "provider_profile_id": None if harness else settings["profile_id"],
            "model": settings["model"],
        }

    @staticmethod
    def normalize_metadata(session, metadata, binding):
        metadata = dict(metadata)
        metadata.pop("pending_runtime_switch", None)
        backend = binding["backend"]
        metadata["assistant_config"] = {
            **metadata.get("assistant_config", {}),
            "backend": backend.value if isinstance(backend, ChatBackend) else backend,
            "profile_id": binding["harness_profile_id"]
            if backend == ChatBackend.HARNESS
            else binding["provider_profile_id"],
            "model": binding["model"],
        }
        if (
            session.backend == ChatBackend.HARNESS
            and binding["backend"] == ChatBackend.PROVIDER
        ):
            metadata.pop("provider_subagent", None)
            reset = {
                "allow_subagents": False,
                "mcp_server_ids": [],
                "hook_ids": [],
                "reasoning_effort": None,
                "harness_reasoning_effort": None,
                "harness_service_tier": None,
                "harness_mode": None,
                "harness_cyber_access_program": None,
            }
            metadata.update(
                {k: v for k, v in reset.items() if not k.startswith("harness_")}
            )
            metadata["assistant_config"] = {
                **metadata.get("assistant_config", {}),
                **reset,
            }
        return metadata

    @staticmethod
    def metadata_changes(before: dict, after: dict) -> list[dict]:
        """Persist selection changes rather than a copy of the chat's state."""
        changes = []

        def compare(old, new, prefix=()):
            for key in sorted(old.keys() | new.keys()):
                if not prefix and key == "pending_runtime_switch":
                    continue
                if key == "assistant_config" and not prefix:
                    compare(old.get(key, {}), new.get(key, {}), (key,))
                elif (key in old) != (key in new) or old.get(key) != new.get(key):
                    changes.append(
                        {
                            "path": [*prefix, key],
                            "before_present": key in old,
                            "before": deepcopy(old.get(key)),
                            "after_present": key in new,
                            "after": deepcopy(new.get(key)),
                        }
                    )

        compare(before, after)
        return changes

    @staticmethod
    def pending_metadata(session, pending):
        changes = pending.get("metadata_changes")
        if not isinstance(changes, list):
            raise ValueError(
                "Review and save this older pending runtime selection again."
            )
        metadata = deepcopy(session.metadata)
        metadata.pop("pending_runtime_switch", None)
        for change in changes:
            path = change["path"]
            if len(path) not in {1, 2} or (
                len(path) == 2 and path[0] != "assistant_config"
            ):
                raise ValueError("Review this invalid pending runtime selection.")
            target = metadata
            if len(path) == 2:
                target = metadata.setdefault("assistant_config", {})
            key = path[-1]
            actual = (key in target, target.get(key))
            before = (change["before_present"], change["before"])
            after = (change["after_present"], change["after"])
            if actual != before and actual != after:
                raise ValueError(
                    "Assistant settings changed while waiting. Review the runtime selection."
                )
            if change["after_present"]:
                target[key] = deepcopy(change["after"])
            else:
                target.pop(key, None)
        return metadata

    async def selection_dependencies(self, session, selected) -> dict[str, int]:
        """Fence the database records read by selection validation at commit."""
        from .domain import Engagement, HarnessProfile, McpServerProfile, ScopePolicy

        revisions = {session.id: session.revision}

        async def read(model, entity_id):
            entity = await self.store.get(model, entity_id)
            revisions[entity.id] = entity.revision
            return entity

        project = await read(Engagement, session.engagement_id)
        if project.scope_policy_id:
            await read(ScopePolicy, project.scope_policy_id)
        await read(
            HarnessProfile if selected["backend"] == "harness" else ProviderProfile,
            selected["profile_id"],
        )
        for identity in selected["mcp_server_ids"]:
            await read(McpServerProfile, identity)
        if selected["allow_subagents"]:
            for key, model in (
                ("subagent_provider_id", ProviderProfile),
                ("subagent_harness_profile_id", HarnessProfile),
            ):
                if selected[key]:
                    await read(model, selected[key])
        return revisions

    async def select(
        self,
        session: ChatSession,
        metadata: dict,
        binding: dict,
        *,
        expected_revision: int,
        confirmation_token=None,
        max_output_tokens=None,
        confirmation_revision=None,
    ) -> ChatSession:
        from .chat import ChatConfigurationError, ChatRuntimeSwitchPreflightRequest

        if session.metadata.get("side_chat") or session.metadata.get(
            "temporary_assistant"
        ):
            raise ChatConfigurationError(
                "Switch the runtime from a saved main conversation."
            )
        if session.metadata.get("archived_at"):
            raise ChatConfigurationError(
                "Unarchive this conversation before switching its runtime."
            )
        from .chat_assistant_config import (
            chat_assistant_config,
            validate_chat_assistant_selection,
        )

        binding = {**binding, "backend": ChatBackend(binding["backend"])}
        metadata = self.normalize_metadata(session, metadata, binding)
        selected_session = session.model_copy(update={**binding, "metadata": metadata})
        selected = await chat_assistant_config(self.store, selected_session)
        dependencies = await self.selection_dependencies(session, selected)
        await validate_chat_assistant_selection(
            self.store,
            selected_session,
            selected,
            workspace_resolver=self.chat.workspace_resolver,
        )
        if binding["backend"] == ChatBackend.PROVIDER:
            preflight = await self.chat.runtime_switch_preflight(
                session.id,
                ChatRuntimeSwitchPreflightRequest(
                    provider_id=binding["provider_profile_id"],
                    model=binding["model"],
                    tools_enabled=bool(metadata.get("tools_enabled")),
                    expected_session_revision=expected_revision,
                    max_output_tokens=max_output_tokens,
                ),
                _confirmation_revision=confirmation_revision,
            )
            if not preflight.compatible:
                raise ChatConfigurationError(
                    preflight.reason or "The selected provider is incompatible."
                )
            if (
                preflight.requires_compaction_confirmation
                and confirmation_token != preflight.confirmation_token
            ):
                raise ChatConfigurationError(
                    "Review and confirm context compaction before switching runtimes."
                )
        async with self.store.transaction(immediate=True) as transaction:
            rows = (
                await transaction.session.scalars(
                    select(EntityRow)
                    .where(EntityRow.id.in_(dependencies))
                    .order_by(EntityRow.id)
                    .with_for_update()
                )
            ).all()
            if len(rows) != len(dependencies) or any(
                row.revision != dependencies[row.id] for row in rows
            ):
                raise ConflictError(
                    "Runtime settings changed during validation; review the selection again."
                )
            row = next((item for item in rows if item.id == session.id), None)
            if row is None or row.revision != expected_revision:
                raise ConflictError(
                    "Conversation changed; reload its runtime settings."
                )
            current = ChatSession.model_validate(row.payload)
            reason = await self.busy_reason(transaction.session, current)
            # Provider turn snapshots already protect an in-flight response from
            # same-provider model/effort changes, retaining that existing behavior.
            same_provider = (
                current.backend == binding["backend"] == ChatBackend.PROVIDER
                and current.provider_profile_id == binding["provider_profile_id"]
            )
            if reason and not same_provider:
                desired = {
                    "id": uuid4().hex,
                    "status": "waiting_for_idle",
                    "reason": reason,
                    "binding": {
                        k: (v.value if isinstance(v, ChatBackend) else v)
                        for k, v in binding.items()
                    },
                    "metadata_changes": self.metadata_changes(
                        current.metadata, metadata
                    ),
                    "confirmation_token": confirmation_token,
                    "confirmation_revision": expected_revision,
                    "activation_revision": expected_revision + 1,
                    "max_output_tokens": max_output_tokens,
                }
                changes = {
                    "metadata": {**current.metadata, "pending_runtime_switch": desired}
                }
            else:
                changes = {**binding, "metadata": metadata}
            saved = await transaction.update(
                ChatSession, session.id, changes, expected_revision=expected_revision
            )
        if saved.metadata.get("pending_runtime_switch"):
            self._pending.add(session.id)
            self.schedule({session.id})
        else:
            self._pending.discard(session.id)
        return saved

    async def _review(self, session, pending, reason):
        pending = {**pending, "status": "needs_review", "reason": reason}
        try:
            await self.store.update(
                ChatSession,
                session.id,
                {"metadata": {**session.metadata, "pending_runtime_switch": pending}},
                expected_revision=session.revision,
            )
            self._pending.discard(session.id)
        except (
            ConflictError
        ):  # diagnostic-expected: a newer operator choice owns the next activation.
            return

    async def settle(
        self, session_id: str, *, explicit: bool = False
    ) -> ChatSession | None:
        from .chat import ChatConfigurationError

        try:
            session = await self.store.get(ChatSession, session_id)
        except NotFoundError:  # diagnostic-expected: deleting a conversation also discards its pending switch.
            self._pending.discard(session_id)
            return None
        pending = session.metadata.get("pending_runtime_switch")
        if not isinstance(pending, dict):
            self._pending.discard(session_id)
            saved = session.metadata.get("assistant_config")
            if (
                explicit
                and isinstance(saved, dict)
                and saved.get("backend") != session.backend.value
            ):
                from .chat_assistant_config import (
                    ChatAssistantConfigPatch,
                    update_chat_assistant_config,
                )

                session = await update_chat_assistant_config(
                    self.store,
                    session,
                    ChatAssistantConfigPatch(
                        backend=saved["backend"],
                        profile_id=saved["profile_id"],
                        model=saved["model"],
                    ),
                    expected_revision=session.revision,
                    workspace_resolver=self.chat.workspace_resolver,
                )
                return (
                    None if session.metadata.get("pending_runtime_switch") else session
                )
            return session
        if pending.get("status") == "needs_review":
            return None
        if await self.reason(session):
            return None
        binding = pending["binding"]
        # A queued harness selection was validated by the shared settings writer;
        # its advertised profile/model must still be available at activation.
        try:
            metadata = self.pending_metadata(session, pending)
            if binding["backend"] == "harness":
                from .domain import HarnessProfile

                profile = await self.store.get(
                    HarnessProfile, binding["harness_profile_id"]
                )
                if (
                    not profile.enabled
                    or binding["model"] not in profile.capabilities.models
                ):
                    await self._review(
                        session,
                        pending,
                        "The selected harness/model changed. Review the runtime switch.",
                    )
                    return None
            saved = await self.select(
                session,
                metadata,
                binding,
                expected_revision=session.revision,
                confirmation_token=pending.get("confirmation_token"),
                max_output_tokens=pending.get("max_output_tokens"),
                confirmation_revision=pending.get("confirmation_revision")
                if session.revision == pending.get("activation_revision")
                else None,
            )
            return None if saved.metadata.get("pending_runtime_switch") else saved
        except ConflictError:  # diagnostic-expected: another turn or runtime selection won the revision claim.
            return None
        except (ChatConfigurationError, NotFoundError, ValueError) as exc:
            # diagnostic-expected: invalidated target/consent is retained for explicit review, never executed on an old backend.
            await self._review(session, pending, str(exc))
            return None
