"""Core-owned headless security identity, separate from the visible desktop page.

This service only prepares the identity and Chromium context. Network capture and
research execution must be supplied by the supervised security proxy/worker before
the Security Tools MCP is enabled. No renderer owns this lifecycle.
"""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

from .browser_host import ManagedBrowserHost
from .browserd import BrowserdSecurityRequest, BrowserdSecurityResponse
from .browser_research import (
    AttackStateRequest,
    BrowserResearchService,
    CrawlStateRequest,
    RepeaterStateRequest,
)
from .browser_security import BrowserSecurityService
from .diagnostics import record_caught_exception
from .domain import (
    BrowserIdentity,
    BrowserAttack,
    BrowserCrawlJob,
    BrowserInterceptItem,
    BrowserRepeaterTab,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTabState,
    Engagement,
    RiskClass,
)
from .storage import NebulaStore


class AgentSecuritySessions:
    _locks: WeakKeyDictionary = WeakKeyDictionary()

    def __init__(self, store: NebulaStore, host: ManagedBrowserHost) -> None:
        if not host.headless or host.store is not store:
            raise ValueError("Agent security requires its own Core-owned headless host")
        self.store = store
        self.host = host
        self._project_locks = self._locks.setdefault(store, {})
        artifact_store = getattr(host, "artifact_store", None)
        self.security = BrowserSecurityService(store, artifact_store)
        self.research = BrowserResearchService(store, self.security, artifact_store)

    async def ensure(self, project_id: str) -> BrowserSession:
        """Create or reconnect one project-isolated headless identity.

        The Core scope and proxy are checked before or during Chromium startup.
        A failed startup leaves a paused, durable session that can be retried.
        """
        async with self._project_locks.setdefault(project_id, asyncio.Lock()):
            self.store.get(Engagement, project_id)
            self.security._scope(project_id)
            existing = self.store.find_entities(
                BrowserSession,
                {"runtime_kind": "agent_security"},
                engagement_id=project_id,
                newest_first=True,
            )
            session = next(
                (
                    item
                    for item in existing
                    if item.status != BrowserSessionStatus.CLOSED
                ),
                None,
            )
            if session is None:
                identity = BrowserIdentity(
                    engagement_id=project_id,
                    name="Agent security",
                    metadata={"runtime_kind": "agent_security"},
                )
                session = BrowserSession(
                    engagement_id=project_id,
                    identity_id=identity.id,
                    name="Agent security",
                    runtime_kind="agent_security",
                    metadata={"execution_owner": "core"},
                )
                for entity, event_type in (
                    (identity, "agent_security.identity_created"),
                    (session, "agent_security.session_created"),
                ):
                    self.store.create_with_operation_event(
                        entity,
                        operation_id=entity.id,
                        operation_kind=entity.entity_kind,
                        engagement_id=project_id,
                        event_type=event_type,
                        event_payload={"entity_id": entity.id},
                        actor_id="core",
                        idempotency_key=f"create:{entity.id}",
                    )
            try:
                adapter = await self.host.adapter()
                response = await adapter._request(
                    "POST",
                    "/v1/identities/ensure",
                    {"identity_id": session.identity_id},
                )
                response.raise_for_status()
                tab_ids = response.json()["tab_ids"]
                if not isinstance(tab_ids, list) or not tab_ids or len(tab_ids) > 16:
                    raise ValueError("The headless browser returned no usable tab")
            except Exception as exc:
                latest = self.store.get(BrowserSession, session.id)
                self.store.update_with_operation_event(
                    BrowserSession,
                    session.id,
                    {
                        "status": BrowserSessionStatus.PAUSED.value,
                        "metadata": {
                            **latest.metadata,
                            "last_start_error": str(exc)[:500],
                        },
                    },
                    expected_revision=latest.revision,
                    operation_id=session.id,
                    operation_kind="browser_session",
                    engagement_id=project_id,
                    event_type="agent_security.session_start_failed",
                    event_payload={"reason": type(exc).__name__},
                    actor_id="core",
                )
                raise
            latest = self.store.get(BrowserSession, session.id)
            updated, _ = self.store.update_with_operation_event(
                BrowserSession,
                session.id,
                {
                    "status": BrowserSessionStatus.ACTIVE.value,
                    "tabs": [
                        BrowserTabState(id=tab_id, position=index).model_dump(
                            mode="json"
                        )
                        for index, tab_id in enumerate(tab_ids)
                    ],
                    "active_tab_id": tab_ids[0],
                    "metadata": {
                        key: value
                        for key, value in latest.metadata.items()
                        if key != "last_start_error"
                    },
                },
                expected_revision=latest.revision,
                operation_id=session.id,
                operation_kind="browser_session",
                engagement_id=project_id,
                event_type="agent_security.session_ready",
                event_payload={"tab_count": len(tab_ids)},
                actor_id="core",
            )
            return updated

    async def request(
        self, project_id: str, request: BrowserdSecurityRequest
    ) -> BrowserdSecurityResponse:
        """Run one headless HTTP request after a fresh Core scope decision."""
        sessions = self.store.find_entities(
            BrowserSession,
            {"identity_id": request.identity_id, "runtime_kind": "agent_security"},
            engagement_id=project_id,
            limit=1,
        )
        if not sessions:
            raise ValueError("security request identity belongs to another Project")
        session = sessions[0]
        if session.status != BrowserSessionStatus.ACTIVE:
            raise ValueError("the agent security session has stopped")
        self.security._require_in_scope(
            self.security._scope(project_id),
            request.url,
            "agent_security.request",
            RiskClass.ACTIVE_SCAN,
            native_scope_authority=True,
        )
        adapter = await self.host.adapter()
        response = await adapter._request(
            "POST", "/v1/security/request", request.model_dump(mode="json")
        )
        response.raise_for_status()
        return BrowserdSecurityResponse.model_validate(response.json())

    async def stop(self, project_id: str, *, reason: str) -> int:
        """Close every live headless context for a Project and durably pause it."""
        async with self._project_locks.setdefault(project_id, asyncio.Lock()):
            self.store.get(Engagement, project_id)
            stopped = 0
            for session in self.store.find_entities(
                BrowserSession,
                {"runtime_kind": "agent_security"},
                engagement_id=project_id,
            ):
                if session.status == BrowserSessionStatus.CLOSED:
                    continue
                try:
                    await self.host.close_identity(session.identity_id)
                except Exception as exc:
                    record_caught_exception(
                        "security_browser",
                        "security_browser.identity_close_failed",
                        "The headless security identity could not close cleanly; the host is being stopped.",
                        exc,
                        stage="agent-security-stop",
                    )
                    # A dead private bridge cannot keep its proxy or Chromium
                    # context alive. Close the whole security host fail-closed.
                    await self.host.close()
                for attack in self.store.find_entities(
                    BrowserAttack,
                    {"session_id": session.id},
                    engagement_id=project_id,
                ):
                    if attack.state in {"queued", "running", "paused"}:
                        self.research.transition_attack(
                            attack.id,
                            AttackStateRequest(
                                expected_revision=attack.revision,
                                action="cancel",
                                actor_id="core",
                                error=reason[:3000],
                            ),
                        )
                for crawl in self.store.find_entities(
                    BrowserCrawlJob,
                    {"session_id": session.id},
                    engagement_id=project_id,
                ):
                    if crawl.state in {"queued", "running", "paused"}:
                        self.research.transition_crawl(
                            crawl.id,
                            CrawlStateRequest(
                                expected_revision=crawl.revision,
                                action="cancel",
                                actor_id="core",
                                error=reason[:3000],
                            ),
                        )
                for tab in self.store.find_entities(
                    BrowserRepeaterTab,
                    {"session_id": session.id},
                    engagement_id=project_id,
                ):
                    if tab.state in {"queued", "running"}:
                        self.research.transition_repeater_tab(
                            tab.id,
                            RepeaterStateRequest(
                                expected_revision=tab.revision,
                                action="cancel",
                                actor_id="core",
                                error=reason[:3000],
                            ),
                        )
                for item in self.store.find_entities(
                    BrowserInterceptItem,
                    {"session_id": session.id, "state": "paused"},
                    engagement_id=project_id,
                ):
                    self.research._transition_intercept(
                        item,
                        item.revision,
                        "interrupted",
                        "core",
                        reason[:3000],
                    )
                if session.status == BrowserSessionStatus.PAUSED:
                    continue
                self.store.update_with_operation_event(
                    BrowserSession,
                    session.id,
                    {
                        "status": BrowserSessionStatus.PAUSED.value,
                        "metadata": {**session.metadata, "stop_reason": reason[:500]},
                    },
                    expected_revision=session.revision,
                    operation_id=session.id,
                    operation_kind="browser_session",
                    engagement_id=project_id,
                    event_type="agent_security.session_stopped",
                    event_payload={"reason": reason[:500]},
                    actor_id="core",
                )
                stopped += 1
            return stopped
