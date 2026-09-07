"""Core-owned bindings for the interactive browser workspace.

This surface deliberately has no scanner, replay, or arbitrary script operations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal, ClassVar
from weakref import WeakKeyDictionary
from urllib.parse import quote

from pydantic import Field

from .browser_engine import BrowserEngineRegistry, LocalBrowserdAdapter
from .browser_security import BrowserSecurityService
from .domain import (
    BrowserIdentity,
    BrowserSession,
    ChatSession,
    Engagement,
    NebulaModel,
    RiskClass,
    CompanionRequest,
    CompanionAction,
)
from .storage import NebulaStore


class CompanionBindingRequest(NebulaModel):
    conversation_id: str | None = Field(default=None, max_length=200)


class CompanionDecision(NebulaModel):
    decision: Literal["approve", "reject"]


class BrowserCompanion:
    _store_locks: ClassVar[WeakKeyDictionary] = WeakKeyDictionary()

    def __init__(self, store: NebulaStore, engines: BrowserEngineRegistry):
        self.store = store
        self.engines = engines
        self.security = BrowserSecurityService(store)
        self._locks: dict[str, asyncio.Lock] = self._store_locks.setdefault(store, {})

    async def adapter(self) -> LocalBrowserdAdapter:
        adapter = await self.engines.adapter("managed-chromium")
        if not isinstance(adapter, LocalBrowserdAdapter):
            raise ValueError(
                "Managed Chromium is unavailable. Prepare the browser runtime on the Nebula host, then retry. Your saved conversations remain available."
            )
        return adapter

    def session(self, session_id: str) -> BrowserSession:
        session = self.store.get(BrowserSession, session_id)
        if session.metadata.get("browser_companion_version") != 1:
            raise ValueError("This session belongs to the native browser.")
        if session.status.value == "closed":
            raise ValueError("This browser session is closed.")
        identity = self.store.get(BrowserIdentity, session.identity_id)
        if identity.engagement_id != session.engagement_id or identity.revoked_at:
            raise ValueError(
                "The browser identity is revoked or belongs to another project."
            )
        return session

    async def open(self, project_id: str) -> dict[str, Any]:
        async with self._locks.setdefault(project_id, asyncio.Lock()):
            self.store.get(Engagement, project_id)
            adapter = await self.adapter()
            sessions = self.store.list_entities(
                BrowserSession, engagement_id=project_id, limit=1000
            )
            session = next(
                (
                    item
                    for item in sessions
                    if item.metadata.get("browser_companion_version") == 1
                ),
                None,
            )
            if session is None:
                identity = BrowserIdentity(
                    engagement_id=project_id,
                    name="Assistant browser",
                    metadata={"browser_companion_version": 1},
                )
                session = BrowserSession(
                    engagement_id=project_id,
                    identity_id=identity.id,
                    name="Assistant browser",
                    metadata={"browser_companion_version": 1},
                )
                self.store.create_many([identity, session])
            self.session(session.id)
            await adapter.ensure_identity(session.identity_id)
            tabs = await self.request(session.id, CompanionRequest(operation="tabs"))
            session = self.session(session.id)
            available = {tab["id"] for tab in tabs["tabs"]}
            page_state_reset = bool(
                session.active_tab_id and session.active_tab_id not in available
            )
            if session.active_tab_id not in available and tabs["tabs"]:
                session = self.store.update(
                    BrowserSession,
                    session.id,
                    {"active_tab_id": tabs["tabs"][0]["id"]},
                    expected_revision=session.revision,
                )
            return {
                "session_id": session.id,
                "conversation_id": session.metadata.get("conversation_id"),
                "active_tab_id": session.active_tab_id,
                "page_state_reset": page_state_reset,
                **tabs,
            }

    async def select_tab(self, session_id: str, tab_id: str) -> None:
        tabs = await self.request(session_id, CompanionRequest(operation="tabs"))
        if not any(tab["id"] == tab_id for tab in tabs["tabs"]):
            raise ValueError("The selected browser tab is no longer available.")
        session = self.session(session_id)
        self.store.update(
            BrowserSession,
            session.id,
            {"active_tab_id": tab_id},
            expected_revision=session.revision,
        )

    def bind(self, session_id: str, conversation_id: str | None) -> None:
        session = self.session(session_id)
        if session.metadata.get("conversation_id") == conversation_id:
            return
        if conversation_id:
            conversation = self.store.get(ChatSession, conversation_id)
            if conversation.engagement_id != session.engagement_id:
                raise ValueError("The conversation belongs to another project.")
        if session.metadata.get("conversation_id"):
            self.takeover(session_id, True)
            session = self.session(session_id)
        self.store.update(
            BrowserSession,
            session.id,
            {"metadata": {**session.metadata, "conversation_id": conversation_id}},
            expected_revision=session.revision,
        )

    async def request(
        self, session_id: str, request: CompanionRequest, *, assistant: bool = False
    ) -> dict[str, Any]:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            session = self.session(session_id)
            adapter = await self.adapter()
            if request.operation not in {"tabs", "navigate", "new_tab", "close_tab"}:
                tabs = await adapter._request(
                    "POST",
                    "/v1/companion/" + quote(session.identity_id, safe=""),
                    {"operation": "tabs"},
                )
                tabs.raise_for_status()
                tab = next(
                    (
                        item
                        for item in tabs.json()["tabs"]
                        if item["id"] == request.tab_id
                    ),
                    None,
                )
                if not tab:
                    raise ValueError("The browser tab is no longer available.")
                self.security._require_in_scope(
                    self.security._scope(session.engagement_id),
                    tab["url"],
                    "browser.read",
                    RiskClass.PASSIVE,
                )
            if request.operation == "navigate":
                if not request.url:
                    raise ValueError("Enter a page URL.")
                self.security._require_in_scope(
                    self.security._scope(session.engagement_id),
                    request.url,
                    "browser.navigate",
                    RiskClass.PASSIVE,
                )
            if assistant and self.session(session_id).metadata.get(
                "assistant_paused", True
            ):
                raise ValueError("Assistant control was paused before execution.")
            response = await adapter._request(
                "POST",
                "/v1/companion/" + quote(session.identity_id, safe=""),
                request.model_dump(),
            )
            if response.is_error:
                raise ValueError(
                    "The browser operation could not complete. Refresh the page context and retry; no action is replayed automatically."
                )
            result = response.json()
            if result.get("active_tab_id"):
                latest = self.session(session_id)
                self.store.update(
                    BrowserSession,
                    session_id,
                    {"active_tab_id": result["active_tab_id"]},
                    expected_revision=latest.revision,
                )
            return result

    def actions(self, session_id: str) -> list[CompanionAction]:
        session = self.session(session_id)
        return [
            item
            for item in self.store.list_entities(
                CompanionAction, engagement_id=session.engagement_id, limit=1000
            )
            if item.browser_session_id == session_id
        ]

    def propose(self, session_id: str, request: CompanionRequest) -> CompanionAction:
        session = self.session(session_id)
        if session.metadata.get("assistant_paused", True):
            raise ValueError(
                "Browser control is paused. Resume assistant control in the browser."
            )
        if not request.page_revision:
            raise ValueError("Capture the current page before proposing an action.")
        return self.store.create(
            CompanionAction(
                engagement_id=session.engagement_id,
                browser_session_id=session.id,
                request=request,
            )
        )

    def takeover(self, session_id: str, paused: bool) -> None:
        session = self.session(session_id)
        if session.metadata.get("assistant_paused", True) != paused:
            self.store.update(
                BrowserSession,
                session_id,
                {"metadata": {**session.metadata, "assistant_paused": paused}},
                expected_revision=session.revision,
            )
        if paused:
            for action in self.actions(session_id):
                if action.status == "pending":
                    self.store.update(
                        CompanionAction,
                        action.id,
                        {"status": "revoked"},
                        expected_revision=action.revision,
                    )

    async def decide(
        self, session_id: str, action_id: str, decision: str
    ) -> CompanionAction:
        session = self.session(session_id)
        action = self.store.get(CompanionAction, action_id)
        if (
            action.browser_session_id != session_id
            or action.engagement_id != session.engagement_id
        ):
            raise ValueError("Action belongs to another browser session.")
        if action.status != "pending":
            raise ValueError(
                "This action has already been decided; it will not be replayed."
            )
        if (
            decision != "approve"
            or action.expires_at <= datetime.now(timezone.utc)
            or session.metadata.get("assistant_paused", True)
        ):
            return self.store.update(
                CompanionAction,
                action.id,
                {"status": "revoked"},
                expected_revision=action.revision,
            )
        running = self.store.update(
            CompanionAction,
            action.id,
            {"status": "running"},
            expected_revision=action.revision,
        )
        try:
            result = await self.request(session_id, action.request, assistant=True)
        except Exception:
            self.store.update(
                CompanionAction,
                action.id,
                {"status": "failed"},
                expected_revision=running.revision,
            )
            raise
        return self.store.update(
            CompanionAction,
            action.id,
            {"status": "complete", "result": result},
            expected_revision=running.revision,
        )
