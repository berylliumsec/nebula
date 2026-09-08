"""Core-owned bindings for the interactive browser workspace.

This surface deliberately has no scanner, replay, or arbitrary script operations.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal, ClassVar
from weakref import WeakKeyDictionary, ref
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from pydantic import Field, SecretStr

from .browser_engine import BrowserEngineRegistry, LocalBrowserdAdapter
from .browser_security import BrowserSecurityService
from .domain import (
    Artifact,
    BrowserIdentity,
    BrowserSession,
    AutomationApprovalPolicy,
    AutomationProjectPolicy,
    Observation,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    NebulaModel,
    RiskClass,
    CompanionRequest,
    CompanionAction,
)
from .artifacts import ArtifactStore
from .storage import NebulaStore
from .credentials import CredentialStore, CredentialCreateRequest


class CompanionBindingRequest(NebulaModel):
    conversation_id: str | None = Field(default=None, max_length=200)


class CompanionDecision(NebulaModel):
    decision: Literal["approve", "reject"]


class CompanionCredentialCreate(CredentialCreateRequest):
    label: str = Field(min_length=1, max_length=100)
    persistence: Literal["vault", "session"] = "session"


class CompanionFileCreate(NebulaModel):
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\\x00-\x1f]+$")
    media_type: str = Field(
        default="application/octet-stream",
        max_length=100,
        pattern=r"^[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+$",
    )
    content_base64: SecretStr = Field(max_length=5592408)


class BrowserCompanion:
    _store_locks: ClassVar[WeakKeyDictionary] = WeakKeyDictionary()
    _store_credentials: ClassVar[WeakKeyDictionary] = WeakKeyDictionary()
    _store_artifacts: ClassVar[WeakKeyDictionary] = WeakKeyDictionary()
    _store_hosts: ClassVar[WeakKeyDictionary] = WeakKeyDictionary()

    def __init__(
        self,
        store: NebulaStore,
        engines: BrowserEngineRegistry,
        *,
        credentials: CredentialStore | None = None,
        artifact_store: ArtifactStore | None = None,
        managed_host: Any | None = None,
    ):
        self.store = store
        self.engines = engines
        self.security = BrowserSecurityService(store)
        self._locks: dict[str, asyncio.Lock] = self._store_locks.setdefault(store, {})
        if credentials is not None:
            self._store_credentials[store] = credentials
        elif store not in self._store_credentials:
            self._store_credentials[store] = CredentialStore()
        self.credentials = self._store_credentials[store]
        if artifact_store is not None:
            self._store_artifacts[store] = artifact_store
        self.artifact_store = self._store_artifacts.get(store)
        if managed_host is not None:
            self._store_hosts[store] = ref(managed_host)

    @staticmethod
    def _safe_url(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host += f":{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return None

    def _record_interaction(
        self,
        session: BrowserSession,
        request: CompanionRequest,
        result: dict[str, Any],
        *,
        assistant: bool,
        chat_turn_id: str | None,
    ) -> None:
        if request.operation == "tabs":
            return
        # Shared Chromium is a first-class recorded source. Ensure it has a
        # durable collection before storing the observation so the transactional
        # outbox can route this interaction immediately. The first collection
        # also imports any earlier durable companion observations.
        from .application_model.service import ApplicationModelService

        ApplicationModelService(self.store).ensure_browser_collection(
            session.engagement_id, session.id
        )
        url = self._safe_url(result.get("url") or request.url)
        metadata = {
            "browser_session_id": session.id,
            "identity_id": session.identity_id,
            "tab_id": request.tab_id or result.get("active_tab_id"),
            "chat_turn_id": chat_turn_id,
            "operation": request.operation,
            "capture_kind": request.capture_kind,
            "status": "complete",
            "url": url,
            "page_revision": result.get("page_revision"),
            "element_count": len(result.get("elements", [])),
            "assistant": assistant,
        }
        if self.artifact_store is not None:
            stored = self.artifact_store.put_bytes_with_status(
                json.dumps(result, sort_keys=True).encode(),
                engagement_id=session.engagement_id,
                filename=f"shared-chromium-{request.operation}.json",
                media_type="application/json",
                source="browser_companion_capture",
                metadata={
                    "browser_session_id": session.id,
                    "tab_id": request.tab_id or result.get("active_tab_id"),
                    "operation": request.operation,
                    "contains_unredacted_page_content": True,
                },
            )
            artifact = self.store.create(stored.artifact)
            metadata["artifact_id"] = artifact.id
        self.store.create(
            Observation(
                engagement_id=session.engagement_id,
                observation_type="browser_companion_interaction",
                title=f"Shared Chromium {request.operation}",
                source="browser_companion",
                metadata={
                    key: value for key, value in metadata.items() if value is not None
                },
            )
        )

    def approval_policy(self, project_id: str) -> AutomationApprovalPolicy:
        policies = self.store.list_entities(
            AutomationProjectPolicy, engagement_id=project_id, limit=2
        )
        return (
            policies[0].approval_policy
            if policies
            else AutomationApprovalPolicy.ON_BOUNDARY
        )

    def file_catalog(self, session_id: str) -> list[dict[str, Any]]:
        session = self.session(session_id)
        return [
            {
                "reference": alias,
                "filename": item["filename"],
                "size": item["size"],
                "media_type": item["media_type"],
            }
            for alias, item in session.metadata.get("browser_files", {}).items()
        ]

    def save_file(
        self, session_id: str, request: CompanionFileCreate
    ) -> list[dict[str, Any]]:
        session = self.session(session_id)
        if self.artifact_store is None:
            raise ValueError(
                "File storage is unavailable. Your saved conversation remains available."
            )
        entries = dict(session.metadata.get("browser_files", {}))
        if len(entries) >= 8 or request.filename in {".", ".."}:
            raise ValueError(
                "Attach at most eight files with ordinary filenames. Remove a file before adding another."
            )
        data = base64.b64decode(
            request.content_base64.get_secret_value(), validate=True
        )
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("Choose a file no larger than 4 MiB.")
        artifact = self.artifact_store.put_bytes(
            data,
            engagement_id=session.engagement_id,
            filename=request.filename,
            media_type=request.media_type,
            source="browser-upload",
            metadata={"browser_session_id": session.id, "sensitive": True},
        )
        entries[uuid4().hex] = {
            "artifact_id": artifact.id,
            "filename": artifact.filename,
            "size": artifact.size,
            "media_type": artifact.media_type,
        }
        with self.store.transaction() as transaction:
            transaction.add(artifact)
            transaction.update(
                BrowserSession,
                session_id,
                {"metadata": {**session.metadata, "browser_files": entries}},
                expected_revision=session.revision,
            )
        return self.file_catalog(session_id)

    def remove_file(self, session_id: str, reference: str) -> list[dict[str, Any]]:
        self.invalidate_pending_actions(session_id)
        session = self.session(session_id)
        entries = dict(session.metadata.get("browser_files", {}))
        if entries.pop(reference, None) is None:
            raise ValueError("This file is no longer attached to the browser.")
        self.store.update(
            BrowserSession,
            session_id,
            {"metadata": {**session.metadata, "browser_files": entries}},
            expected_revision=session.revision,
        )
        return self.file_catalog(session_id)

    def file_payload(self, session_id: str, reference: str | None) -> dict[str, str]:
        session = self.session(session_id)
        entry = session.metadata.get("browser_files", {}).get(reference)
        if not entry or self.artifact_store is None:
            raise ValueError(
                "Choose a file attached to this browser. Removed files require a new attachment and approval."
            )
        artifact = self.store.get(Artifact, entry["artifact_id"])
        if (
            artifact.engagement_id != session.engagement_id
            or artifact.source != "browser-upload"
            or artifact.metadata.get("browser_session_id") != session_id
        ):
            raise ValueError("The file does not belong to this browser session.")
        data = self.artifact_store.read(artifact)
        if (
            len(data) > 4 * 1024 * 1024
            or len(data) != artifact.size
            or hashlib.sha256(data).hexdigest() != artifact.sha256
        ):
            raise ValueError(
                "The attached file failed integrity verification. Remove it and attach it again."
            )
        return {
            "name": artifact.filename or "upload",
            "mime_type": artifact.media_type,
            "content_base64": base64.b64encode(data).decode("ascii"),
        }

    def credential_catalog(self, session_id: str) -> list[dict[str, Any]]:
        session = self.session(session_id)
        return [
            {
                "reference": alias,
                "label": value["label"],
                "available": self.credentials.status(value["reference"]).available,
            }
            for alias, value in session.metadata.get("browser_credentials", {}).items()
        ]

    def save_credential(
        self, session_id: str, request: CompanionCredentialCreate
    ) -> list[dict[str, Any]]:
        session = self.session(session_id)
        entries = dict(session.metadata.get("browser_credentials", {}))
        if any(
            entry["label"].strip().casefold() == request.label.strip().casefold()
            for entry in entries.values()
        ):
            raise ValueError(
                "A protected value with this label already exists. Remove it before saving a replacement."
            )
        if len(entries) >= 16 or len(request.secret.get_secret_value()) > 4000:
            raise ValueError(
                "Use at most 16 browser credentials, each at most 4000 characters."
            )
        credential = self.credentials.create(request)
        entries[uuid4().hex] = {
            "label": request.label,
            "reference": credential.reference,
        }
        try:
            self.store.update(
                BrowserSession,
                session_id,
                {"metadata": {**session.metadata, "browser_credentials": entries}},
                expected_revision=session.revision,
            )
        except Exception:
            # diagnostic-expected: roll back the secret if its association was not saved.
            self.credentials.delete(credential.reference)
            raise
        return self.credential_catalog(session_id)

    def remove_credential(
        self, session_id: str, reference: str
    ) -> list[dict[str, Any]]:
        self.invalidate_pending_actions(session_id)
        session = self.session(session_id)
        entries = dict(session.metadata.get("browser_credentials", {}))
        removed = entries.pop(reference, None)
        if removed is None:
            raise ValueError("This credential is not attached to this browser.")
        self.store.update(
            BrowserSession,
            session_id,
            {"metadata": {**session.metadata, "browser_credentials": entries}},
            expected_revision=session.revision,
        )
        self.credentials.delete(removed["reference"])
        return self.credential_catalog(session_id)

    def protected_values(self, session_id: str) -> dict[str, str]:
        session = self.session(session_id)
        values = {}
        for alias, entry in session.metadata.get("browser_credentials", {}).items():
            if self.credentials.status(entry["reference"]).available:
                values[alias] = self.credentials.resolve(
                    entry["reference"]
                ).get_secret_value()
        return values

    async def adapter(self) -> LocalBrowserdAdapter:
        adapter = await self.engines.adapter("managed-chromium")
        host_ref = self._store_hosts.get(self.store)
        if (
            adapter is None
            and not (
                os.environ.get("NEBULA_BROWSERD_URL")
                or os.environ.get("NEBULA_BROWSERD_TOKEN")
            )
            and not any(
                isinstance(item, LocalBrowserdAdapter)
                for item in self.engines._adapters
            )
            and host_ref is not None
        ):
            host = host_ref()
            if host is not None:
                adapter = await host.adapter()
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
            previous_active_tab = session.active_tab_id
            tabs = await self.request(session.id, CompanionRequest(operation="tabs"))
            session = self.session(session.id)
            available = {tab["id"] for tab in tabs["tabs"]}
            page_state_reset = bool(
                session.metadata.get("browser_page_state_reset")
                or (previous_active_tab and previous_active_tab not in available)
            )
            if page_state_reset:
                self.takeover(session.id, True)
                session = self.session(session.id)
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
        self,
        session_id: str,
        request: CompanionRequest,
        *,
        assistant: bool = False,
        approved: bool = False,
        chat_turn_id: str | None = None,
    ) -> dict[str, Any]:
        if request.operation == "upload" and not approved:
            raise ValueError(
                "File uploads require an inline approval. Propose an upload before execution."
            )
        if not assistant and request.operation not in {"tabs", "capture"}:
            # Manual edits invalidate old approvals, but the explicit session
            # grant remains enabled until the operator stops it.
            self.invalidate_pending_actions(session_id)
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            session = self.session(session_id)
            if not self.turn_active(chat_turn_id):
                raise ValueError(
                    "The originating Assistant turn ended; request a fresh action."
                )
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
            if not self.turn_active(chat_turn_id):
                raise ValueError(
                    "The originating Assistant turn ended; request a fresh action."
                )
            protected = self.protected_values(session_id)
            payload = request.model_dump()
            if request.operation == "upload":
                payload["upload_file"] = self.file_payload(session_id, request.file_ref)
            elif request.file_ref:
                raise ValueError("Attached file references are only valid for uploads.")
            if request.credential_ref:
                if (
                    request.operation != "fill"
                    or request.text
                    or request.credential_ref not in protected
                ):
                    raise ValueError(
                        "Select an available credential attached to this browser; protected fills cannot contain plain text."
                    )
                payload["text"] = protected[request.credential_ref]
            try:
                response = await adapter._request(
                    "POST",
                    "/v1/companion/" + quote(session.identity_id, safe=""),
                    payload,
                )
            except httpx.TimeoutException as exc:
                raise ValueError(
                    "The host browser did not respond in time. Reconnect to check its current page before retrying; no action was replayed."
                ) from exc
            except httpx.TransportError as exc:
                raise ValueError(
                    "The host browser connection ended. Reconnect to restore the view; your conversation is saved."
                ) from exc
            if response.is_error and response.status_code == 504:
                raise ValueError(
                    "The host browser timed out loading or reading the page. Check the page and retry; no action was replayed."
                )
            if response.is_error:
                raise ValueError(
                    "The browser operation could not complete. Refresh the page context and retry; no action is replayed automatically."
                )
            result = response.json()
            self._record_interaction(
                session,
                request,
                result,
                assistant=assistant,
                chat_turn_id=chat_turn_id,
            )
            result["credentials"] = self.credential_catalog(session_id)
            result["files"] = self.file_catalog(session_id)
            if "tabs" in result:
                latest = self.session(session_id)
                tab_ids = {tab["id"] for tab in result["tabs"]}
                known_ids = {tab.id for tab in latest.tabs}
                lost_tabs = (
                    request.operation == "tabs"
                    and bool(known_ids)
                    and known_ids.isdisjoint(tab_ids)
                )
                metadata = dict(latest.metadata)
                if lost_tabs:
                    metadata["browser_page_state_reset"] = True
                active_tab = result.get("active_tab_id") or latest.active_tab_id
                if active_tab not in tab_ids:
                    active_tab = result["tabs"][0]["id"] if result["tabs"] else None
                self.store.update(
                    BrowserSession,
                    session_id,
                    {
                        "active_tab_id": active_tab,
                        "metadata": metadata,
                        "tabs": [
                            {"id": tab["id"], "title": tab["title"], "position": index}
                            for index, tab in enumerate(result["tabs"])
                        ],
                    },
                    expected_revision=latest.revision,
                )
                if lost_tabs:
                    self.takeover(session_id, True)
            if request.operation == "navigate":
                latest = self.session(session_id)
                if latest.metadata.get("browser_page_state_reset"):
                    self.store.update(
                        BrowserSession,
                        latest.id,
                        {
                            "metadata": {
                                **latest.metadata,
                                "browser_page_state_reset": False,
                            }
                        },
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

    def turn_active(self, chat_turn_id: str | None) -> bool:
        if chat_turn_id is None:
            return True
        return self.store.get(ChatTurn, chat_turn_id).status not in {
            ChatTurnStatus.COMPLETE,
            ChatTurnStatus.CANCELLED,
            ChatTurnStatus.FAILED,
            ChatTurnStatus.INTERRUPTED,
        }

    def propose(
        self,
        session_id: str,
        request: CompanionRequest,
        *,
        operator_requested: bool = False,
        chat_turn_id: str | None = None,
    ) -> CompanionAction:
        session = self.session(session_id)
        if session.metadata.get("assistant_paused", True) and not operator_requested:
            raise ValueError(
                "Browser control is paused. Resume assistant control in the browser."
            )
        if not request.page_revision:
            raise ValueError("Capture the current page before proposing an action.")
        if request.operation == "upload":
            self.file_payload(session_id, request.file_ref)
        return self.store.create(
            CompanionAction(
                engagement_id=session.engagement_id,
                browser_session_id=session.id,
                request=request,
                operator_requested=operator_requested,
                chat_turn_id=chat_turn_id,
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
            self.invalidate_pending_actions(session_id)

    def invalidate_pending_actions(self, session_id: str) -> None:
        """Invalidate approvals without changing the operator's control grant."""
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
            not self.turn_active(action.chat_turn_id)
            or decision != "approve"
            or action.expires_at <= datetime.now(timezone.utc)
            or (
                session.metadata.get("assistant_paused", True)
                and not action.operator_requested
            )
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
            result = await self.request(
                session_id,
                action.request,
                assistant=not action.operator_requested,
                approved=True,
                chat_turn_id=action.chat_turn_id,
            )
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
