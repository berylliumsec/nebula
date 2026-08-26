"""Durable Burp-class research primitives for the Nebula security browser.

Live network transactions remain native-owned.  This service persists their
reviewable state, enforces Project/session ownership, and provides bounded,
deterministic utilities that are safe to expose to operators and AI runtimes.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import gzip
import hashlib
import html
import json
import math
from collections import Counter
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, quote, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from .artifacts import ArtifactStore
from .browser_security import (
    BrowserSecurityService,
    BrowserWorkflowError,
    redact_browser_headers,
)
from .domain import (
    BrowserAttack,
    BrowserAttackResult,
    BrowserCrawlJob,
    BrowserInterceptItem,
    BrowserRepeaterTab,
    BrowserSession,
    BrowserSiteEdge,
    BrowserSiteNode,
    BrowserTokenAnalysis,
    BrowserTrafficExchange,
    Evidence,
    Finding,
    FindingStatus,
    NebulaModel,
    Severity,
    utc_now,
)
from .storage import NebulaStore


MAX_UTILITY_INPUT = 1_048_576
MAX_HAR_ENTRIES = 10_000
INTERCEPT_LIFETIME = timedelta(minutes=5)
ALLOWED_TRANSFORMS = {
    "url_encode",
    "url_decode",
    "base64_encode",
    "base64_decode",
    "hex_encode",
    "hex_decode",
    "html_encode",
    "html_decode",
    "lowercase",
    "uppercase",
}
CURATED_PAYLOADS: dict[str, tuple[str, ...]] = {
    "booleans": ("true", "false", "1", "0", "null"),
    "boundary_numbers": ("-1", "0", "1", "2147483647", "2147483648"),
    "path_boundaries": (".", "..", "../", "%2e%2e%2f"),
    "header_boundaries": ("", "0", "null", "undefined"),
}


class BrowserResearchWorkspace(NebulaModel):
    site_nodes: list[BrowserSiteNode]
    site_edges: list[BrowserSiteEdge]
    crawl_jobs: list[BrowserCrawlJob]
    intercepts: list[BrowserInterceptItem]
    repeater_tabs: list[BrowserRepeaterTab]
    attacks: list[BrowserAttack]
    attack_results: list[BrowserAttackResult]
    token_analyses: list[BrowserTokenAnalysis]


class SiteNodeRecordRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=16_384)
    method: str = Field(default="GET", pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    kind: Literal["page", "api", "form", "resource", "websocket"] = "page"
    discovery_source: Literal[
        "browser", "proxy", "crawl", "repeater", "intruder", "har", "automation"
    ] = "browser"
    status_code: int | None = Field(default=None, ge=100, le=999)
    parameter_names: list[str] = Field(default_factory=list, max_length=256)
    content_type: str | None = Field(default=None, max_length=500)
    exchange_id: str | None = Field(default=None, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SiteEdgeRecordRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    source_node_id: str = Field(min_length=1, max_length=200)
    target_node_id: str = Field(min_length=1, max_length=200)
    relation: Literal["navigation", "link", "form", "redirect", "request"]
    discovered_by: Literal["browser", "crawl", "har", "automation"] = "browser"


class CrawlCreateRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    start_url: str = Field(min_length=1, max_length=16_384)
    max_depth: int = Field(default=2, ge=0, le=10)
    max_requests: int = Field(default=100, ge=1, le=10_000)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    max_duration_seconds: int = Field(default=300, ge=1, le=3_600)
    max_body_bytes: int = Field(default=1_048_576, ge=0, le=16_777_216)


class CrawlStateRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    action: Literal["queue", "start", "pause", "resume", "complete", "cancel", "fail"]
    actor_id: str = Field(min_length=1, max_length=200)
    requests_completed: int | None = Field(default=None, ge=0)
    nodes_discovered: int | None = Field(default=None, ge=0)
    checkpoint: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=4_000)


class InterceptCreateRequest(NebulaModel):
    tab_id: str = Field(min_length=1, max_length=200)
    transaction_id: str = Field(min_length=1, max_length=300)
    phase: Literal["request", "response"]
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    status_code: int | None = Field(default=None, ge=100, le=999)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_artifact_id: str | None = Field(default=None, max_length=200)
    timeout_seconds: int = Field(default=60, ge=5, le=300)


class InterceptDecisionRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    decision: Literal["forward", "drop"]
    operator_id: str = Field(min_length=1, max_length=200)
    method: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str | None = Field(default=None, max_length=16_384)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_artifact_id: str | None = Field(default=None, max_length=200)


class RepeaterTabCreateRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="Repeater", min_length=1, max_length=200)
    group: str = Field(default="Ungrouped", max_length=200)
    notes: str = Field(default="", max_length=20_000)
    protocol: Literal["http", "websocket"] = "http"
    method: str = Field(default="GET", pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_artifact_id: str | None = Field(default=None, max_length=200)
    source_exchange_id: str | None = Field(default=None, max_length=200)


class RepeaterTabUpdateRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    group: str = Field(default="Ungrouped", max_length=200)
    notes: str = Field(default="", max_length=20_000)
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url: str = Field(min_length=1, max_length=16_384)
    headers: list[tuple[str, str]] = Field(default_factory=list, max_length=200)
    body_artifact_id: str | None = Field(default=None, max_length=200)


class RepeaterResultRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    exchange_id: str = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=200)


class AttackCreateRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="Intruder attack", min_length=1, max_length=200)
    strategy: Literal["sniper", "battering_ram", "pitchfork", "cluster_bomb"]
    method: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{0,31}$")
    url_template: str = Field(min_length=1, max_length=16_384)
    headers_template: list[tuple[str, str]] = Field(
        default_factory=list, max_length=200
    )
    body_template: str = Field(default="", max_length=65_536)
    positions: list[str] = Field(min_length=1, max_length=32)
    payload_sets: list[dict[str, Any]] = Field(min_length=1, max_length=32)
    transforms: list[str] = Field(default_factory=list, max_length=16)
    max_requests: int = Field(default=100, ge=1, le=100_000)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    requests_per_second: float = Field(default=2.0, gt=0, le=100.0)

    @field_validator("transforms")
    @classmethod
    def transforms_are_declarative(cls, values: list[str]) -> list[str]:
        unknown = set(values) - ALLOWED_TRANSFORMS
        if unknown:
            raise ValueError(f"unsupported Intruder transforms: {sorted(unknown)}")
        return values

    @model_validator(mode="after")
    def payloads_are_inert_and_bounded(self) -> "AttackCreateRequest":
        total = 0
        for item in self.payload_sets:
            kind = item.get("kind")
            if kind == "curated":
                if item.get("name") not in CURATED_PAYLOADS:
                    raise ValueError("unknown curated payload set")
                total += len(CURATED_PAYLOADS[str(item["name"])])
            elif kind == "list":
                values = item.get("values")
                if not isinstance(values, list) or not values or len(values) > 10_000:
                    raise ValueError("custom payload sets require 1-10000 inert values")
                if not all(
                    isinstance(value, str) and len(value) <= 16_384 for value in values
                ):
                    raise ValueError("custom payload values must be bounded strings")
                total += len(values)
            else:
                raise ValueError("payload sets must be curated or inert lists")
        if total > 50_000:
            raise ValueError("combined payload sets exceed the 50000-value limit")
        return self


class AttackStateRequest(NebulaModel):
    expected_revision: int = Field(ge=1)
    action: Literal["queue", "start", "pause", "resume", "cancel", "complete", "fail"]
    actor_id: str = Field(min_length=1, max_length=200)
    error: str | None = Field(default=None, max_length=4_000)


class AttackResultRequest(NebulaModel):
    sequence: int = Field(ge=0)
    payloads: list[str] = Field(default_factory=list, max_length=32)
    exchange_id: str | None = Field(default=None, max_length=200)
    status_code: int | None = Field(default=None, ge=100, le=999)
    response_bytes: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=4_000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)


class DecoderRequest(NebulaModel):
    operation: Literal[
        "url_encode",
        "url_decode",
        "html_encode",
        "html_decode",
        "base64_encode",
        "base64_decode",
        "hex_encode",
        "hex_decode",
        "gzip_compress",
        "gzip_decompress",
        "jwt_inspect",
        "sha256",
        "sha1",
        "md5",
    ]
    value: str = Field(max_length=MAX_UTILITY_INPUT)


class CompareRequest(NebulaModel):
    mode: Literal["text", "bytes", "json", "http"] = "text"
    left: str = Field(max_length=MAX_UTILITY_INPUT)
    right: str = Field(max_length=MAX_UTILITY_INPUT)


class TokenAnalysisRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    name: str = Field(default="Token analysis", min_length=1, max_length=200)
    samples: list[str] = Field(min_length=1, max_length=100_000)
    source_exchange_ids: list[str] = Field(default_factory=list, max_length=1_000)


class FindingPromotionRequest(NebulaModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=100_000)
    severity: Severity = Severity.INFO
    severity_rationale: str = Field(default="", max_length=20_000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    site_node_ids: list[str] = Field(default_factory=list, max_length=100)
    source_exchange_ids: list[str] = Field(default_factory=list, max_length=100)
    run_id: str | None = Field(default=None, max_length=200)


class HarImportRequest(NebulaModel):
    session_id: str = Field(min_length=1, max_length=200)
    har: dict[str, Any]


class BrowserResearchService:
    def __init__(
        self,
        store: NebulaStore,
        security: BrowserSecurityService,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.store = store
        self.security = security
        self.artifact_store = artifact_store

    def workspace(self, engagement_id: str) -> BrowserResearchWorkspace:
        self.security.workspace(engagement_id)
        return BrowserResearchWorkspace(
            site_nodes=self.store.list_entities(
                BrowserSiteNode, engagement_id=engagement_id, limit=1_000
            ),
            site_edges=self.store.list_entities(
                BrowserSiteEdge, engagement_id=engagement_id, limit=1_000
            ),
            crawl_jobs=self.store.list_entities(
                BrowserCrawlJob, engagement_id=engagement_id, limit=1_000
            ),
            intercepts=self.store.list_entities(
                BrowserInterceptItem, engagement_id=engagement_id, limit=1_000
            ),
            repeater_tabs=self.store.list_entities(
                BrowserRepeaterTab, engagement_id=engagement_id, limit=1_000
            ),
            attacks=self.store.list_entities(
                BrowserAttack, engagement_id=engagement_id, limit=1_000
            ),
            attack_results=self.store.list_entities(
                BrowserAttackResult, engagement_id=engagement_id, limit=1_000
            ),
            token_analyses=self.store.list_entities(
                BrowserTokenAnalysis, engagement_id=engagement_id, limit=1_000
            ),
        )

    def create_crawl(
        self, request: CrawlCreateRequest, actor_id: str
    ) -> BrowserCrawlJob:
        session = self.store.get(BrowserSession, request.session_id)
        if session.identity_id != request.identity_id:
            raise BrowserWorkflowError("crawl identity must match the selected session")
        self.security._require_in_scope(
            self.security._scope(session.engagement_id),
            request.start_url,
            "browser.crawl.create",
            self._active_risk(),
            native_scope_authority=True,
        )
        return self._create(
            BrowserCrawlJob(
                engagement_id=session.engagement_id, **request.model_dump()
            ),
            "browser_crawl.created",
            actor_id,
        )

    def transition_crawl(
        self, crawl_id: str, request: CrawlStateRequest
    ) -> BrowserCrawlJob:
        crawl = self.store.get(BrowserCrawlJob, crawl_id)
        transitions = {
            "draft": {"queue": "queued", "cancel": "cancelled"},
            "queued": {"start": "running", "cancel": "cancelled", "fail": "failed"},
            "running": {
                "pause": "paused",
                "complete": "complete",
                "cancel": "cancelled",
                "fail": "failed",
            },
            "paused": {"resume": "running", "cancel": "cancelled", "fail": "failed"},
        }
        next_state = transitions.get(crawl.state, {}).get(request.action)
        if next_state is None:
            raise BrowserWorkflowError(
                f"cannot {request.action} a crawl in {crawl.state} state"
            )
        changes: dict[str, Any] = {"state": next_state, "error": request.error}
        for field in ("requests_completed", "nodes_discovered", "checkpoint"):
            value = getattr(request, field)
            if value is not None:
                changes[field] = value
        if (
            changes.get("requests_completed", crawl.requests_completed)
            > crawl.max_requests
        ):
            raise BrowserWorkflowError("crawl request budget is exhausted")
        if next_state == "running" and crawl.started_at is None:
            changes["started_at"] = utc_now()
        if next_state in {"complete", "cancelled", "failed"}:
            changes["completed_at"] = utc_now()
        updated, _ = self.store.update_with_operation_event(
            BrowserCrawlJob,
            crawl.id,
            changes,
            expected_revision=request.expected_revision,
            operation_id=crawl.id,
            operation_kind="browser_crawl",
            engagement_id=crawl.engagement_id,
            event_type=f"browser_crawl.{next_state}",
            event_payload={"checkpoint": changes.get("checkpoint", crawl.checkpoint)},
            actor_id=request.actor_id,
        )
        return updated

    def record_site_node(
        self, request: SiteNodeRecordRequest, actor_id: str
    ) -> BrowserSiteNode:
        session = self.store.get(BrowserSession, request.session_id)
        scope = self.security._scope(session.engagement_id)
        self.security._require_in_scope(
            scope, request.url, "browser.target.record", self._passive_risk()
        )
        normalized = self._normalized_site_url(request.url)
        existing = next(
            (
                node
                for node in self.store.list_entities(
                    BrowserSiteNode, engagement_id=session.engagement_id, limit=1_000
                )
                if node.session_id == session.id
                and node.method == request.method
                and node.url == normalized
            ),
            None,
        )
        parameters = sorted(
            set(request.parameter_names)
            | {
                name
                for name, _ in parse_qsl(
                    urlsplit(normalized).query, keep_blank_values=True
                )
            }
        )[:256]
        if existing is not None:
            updated, _ = self.store.update_with_operation_event(
                BrowserSiteNode,
                existing.id,
                {
                    "status_code": request.status_code,
                    "parameter_names": parameters,
                    "content_type": request.content_type,
                    "last_exchange_id": request.exchange_id,
                    "last_seen_at": utc_now(),
                    "evidence_ids": list(
                        dict.fromkeys([*existing.evidence_ids, *request.evidence_ids])
                    )[:100],
                    "metadata": {**existing.metadata, **request.metadata},
                },
                expected_revision=existing.revision,
                operation_id=existing.id,
                operation_kind="browser_site_node",
                engagement_id=existing.engagement_id,
                event_type="browser_site_node.observed",
                event_payload={"url": normalized, "source": request.discovery_source},
                actor_id=actor_id,
            )
            return updated
        node = BrowserSiteNode(
            engagement_id=session.engagement_id,
            session_id=session.id,
            identity_id=session.identity_id,
            url=normalized,
            method=request.method,
            kind=request.kind,
            discovery_source=request.discovery_source,
            status_code=request.status_code,
            parameter_names=parameters,
            content_type=request.content_type,
            scope_policy_id=scope.id,
            scope_policy_revision=scope.revision,
            last_exchange_id=request.exchange_id,
            evidence_ids=request.evidence_ids,
            metadata=request.metadata,
        )
        return self._create(node, "browser_site_node.created", actor_id)

    def record_exchange(
        self, exchange: BrowserTrafficExchange, actor_id: str
    ) -> BrowserSiteNode | None:
        if exchange.scope_state != "in_scope":
            return None
        content_type = next(
            (
                value
                for name, value in exchange.response_headers.items()
                if name.lower() == "content-type"
            ),
            None,
        )
        kind: Literal["page", "api", "form", "resource", "websocket"] = (
            "websocket"
            if exchange.protocol == "websocket"
            else (
                "api"
                if content_type
                and ("json" in content_type or "graphql" in content_type)
                else "page"
            )
        )
        return self.record_site_node(
            SiteNodeRecordRequest(
                session_id=exchange.session_id,
                url=exchange.url,
                method=exchange.method,
                kind=kind,
                discovery_source="proxy",
                status_code=exchange.status_code,
                content_type=content_type,
                exchange_id=exchange.id,
            ),
            actor_id,
        )

    def record_site_edge(
        self, request: SiteEdgeRecordRequest, actor_id: str
    ) -> BrowserSiteEdge:
        session = self.store.get(BrowserSession, request.session_id)
        source = self._owned(
            BrowserSiteNode, request.source_node_id, session.engagement_id
        )
        target = self._owned(
            BrowserSiteNode, request.target_node_id, session.engagement_id
        )
        if source.session_id != session.id or target.session_id != session.id:
            raise BrowserWorkflowError(
                "site-map edge endpoints must belong to the selected session"
            )
        existing = next(
            (
                edge
                for edge in self.store.list_entities(
                    BrowserSiteEdge, engagement_id=session.engagement_id, limit=1_000
                )
                if edge.session_id == session.id
                and edge.source_node_id == source.id
                and edge.target_node_id == target.id
                and edge.relation == request.relation
            ),
            None,
        )
        if existing is not None:
            return existing
        return self._create(
            BrowserSiteEdge(
                engagement_id=session.engagement_id, **request.model_dump()
            ),
            "browser_site_edge.created",
            actor_id,
        )

    def pause_intercept(
        self, session_id: str, request: InterceptCreateRequest, actor_id: str
    ) -> BrowserInterceptItem:
        session = self.store.get(BrowserSession, session_id)
        if not session.interception_enabled:
            raise BrowserWorkflowError(
                "interception must be explicitly enabled for the session"
            )
        if request.tab_id not in {tab.id for tab in session.tabs}:
            raise BrowserWorkflowError(
                "intercept tab does not belong to the browser session"
            )
        self.security._require_in_scope(
            self.security._scope(session.engagement_id),
            request.url,
            "browser.intercept",
            self._active_risk(),
            native_scope_authority=True,
        )
        for item in self.store.list_entities(
            BrowserInterceptItem, engagement_id=session.engagement_id, limit=1_000
        ):
            if item.transaction_id == request.transaction_id:
                return item
        item = BrowserInterceptItem(
            engagement_id=session.engagement_id,
            session_id=session.id,
            identity_id=session.identity_id,
            headers=self._redact_pairs(request.headers),
            expires_at=utc_now() + timedelta(seconds=request.timeout_seconds),
            **request.model_dump(exclude={"headers", "timeout_seconds"}),
        )
        return self._create(item, "browser_intercept.paused", actor_id)

    def decide_intercept(
        self, intercept_id: str, request: InterceptDecisionRequest
    ) -> BrowserInterceptItem:
        item = self.store.get(BrowserInterceptItem, intercept_id)
        if item.state != "paused":
            raise BrowserWorkflowError("only paused intercepts can be decided")
        if utc_now() >= item.expires_at:
            return self._transition_intercept(
                item,
                request.expected_revision,
                "expired",
                request.operator_id,
                "intercept expired before decision",
            )
        target = request.url or item.url
        self.security._require_in_scope(
            self.security._scope(item.engagement_id),
            target,
            "browser.intercept.forward",
            self._active_risk(),
            native_scope_authority=True,
        )
        changes: dict[str, Any] = {
            "state": "forwarded" if request.decision == "forward" else "dropped",
            "decision": request.decision,
            "decided_by": request.operator_id,
            "decided_at": utc_now(),
        }
        if request.decision == "forward":
            changes.update(
                {
                    "edited_method": request.method,
                    "edited_url": request.url,
                    "edited_headers": self._redact_pairs(request.headers),
                    "edited_body_artifact_id": request.body_artifact_id,
                }
            )
        updated, _ = self.store.update_with_operation_event(
            BrowserInterceptItem,
            item.id,
            changes,
            expected_revision=request.expected_revision,
            operation_id=item.id,
            operation_kind="browser_intercept",
            engagement_id=item.engagement_id,
            event_type=f"browser_intercept.{changes['state']}",
            event_payload={"decision": request.decision},
            actor_id=request.operator_id,
        )
        return updated

    def interrupt_stale_intercepts(
        self, engagement_id: str, actor_id: str = "system"
    ) -> int:
        changed = 0
        for item in self.store.list_entities(
            BrowserInterceptItem, engagement_id=engagement_id, limit=1_000
        ):
            if item.state == "paused" and utc_now() >= item.expires_at:
                self._transition_intercept(
                    item,
                    item.revision,
                    "interrupted",
                    actor_id,
                    "native transaction expired or disconnected",
                )
                changed += 1
        return changed

    def create_repeater_tab(
        self, request: RepeaterTabCreateRequest, actor_id: str
    ) -> BrowserRepeaterTab:
        session = self.store.get(BrowserSession, request.session_id)
        if session.identity_id != request.identity_id:
            raise BrowserWorkflowError(
                "Repeater identity must match the selected session"
            )
        self.security._require_in_scope(
            self.security._scope(session.engagement_id),
            request.url,
            "browser.repeater.create",
            self._active_risk(),
            native_scope_authority=True,
        )
        if request.source_exchange_id:
            self._owned(
                BrowserTrafficExchange,
                request.source_exchange_id,
                session.engagement_id,
            )
        tab = BrowserRepeaterTab(
            engagement_id=session.engagement_id,
            headers=self._redact_pairs(request.headers),
            **request.model_dump(exclude={"headers"}),
        )
        return self._create(tab, "browser_repeater_tab.created", actor_id)

    def update_repeater_tab(
        self, tab_id: str, request: RepeaterTabUpdateRequest, actor_id: str
    ) -> BrowserRepeaterTab:
        tab = self.store.get(BrowserRepeaterTab, tab_id)
        self.security._require_in_scope(
            self.security._scope(tab.engagement_id),
            request.url,
            "browser.repeater.edit",
            self._active_risk(),
            native_scope_authority=True,
        )
        changes = request.model_dump(exclude={"expected_revision", "headers"})
        changes["headers"] = self._redact_pairs(request.headers)
        updated, _ = self.store.update_with_operation_event(
            BrowserRepeaterTab,
            tab.id,
            changes,
            expected_revision=request.expected_revision,
            operation_id=tab.id,
            operation_kind="browser_repeater_tab",
            engagement_id=tab.engagement_id,
            event_type="browser_repeater_tab.updated",
            event_payload={"url": request.url},
            actor_id=actor_id,
        )
        return updated

    def record_repeater_result(
        self, tab_id: str, request: RepeaterResultRequest
    ) -> BrowserRepeaterTab:
        tab = self.store.get(BrowserRepeaterTab, tab_id)
        exchange = self._owned(
            BrowserTrafficExchange, request.exchange_id, tab.engagement_id
        )
        if exchange.session_id != tab.session_id:
            raise BrowserWorkflowError(
                "Repeater result belongs to another browser session"
            )
        history = list(dict.fromkeys([*tab.history_exchange_ids, exchange.id]))[-500:]
        updated, _ = self.store.update_with_operation_event(
            BrowserRepeaterTab,
            tab.id,
            {"history_exchange_ids": history},
            expected_revision=request.expected_revision,
            operation_id=tab.id,
            operation_kind="browser_repeater_tab",
            engagement_id=tab.engagement_id,
            event_type="browser_repeater_tab.result_recorded",
            event_payload={"exchange_id": exchange.id},
            actor_id=request.actor_id,
        )
        return updated

    def create_attack(
        self, request: AttackCreateRequest, actor_id: str
    ) -> BrowserAttack:
        session = self.store.get(BrowserSession, request.session_id)
        if session.identity_id != request.identity_id:
            raise BrowserWorkflowError(
                "attack identity must match the selected session"
            )
        sample_url = request.url_template
        for position in request.positions:
            sample_url = sample_url.replace(f"§{position}§", "sample")
        self.security._require_in_scope(
            self.security._scope(session.engagement_id),
            sample_url,
            "browser.intruder.create",
            self._active_risk(),
            native_scope_authority=True,
        )
        attack = BrowserAttack(
            engagement_id=session.engagement_id, **request.model_dump()
        )
        return self._create(attack, "browser_attack.created", actor_id)

    def transition_attack(
        self, attack_id: str, request: AttackStateRequest
    ) -> BrowserAttack:
        attack = self.store.get(BrowserAttack, attack_id)
        transitions = {
            "draft": {"queue": "queued", "cancel": "cancelled"},
            "queued": {"start": "running", "cancel": "cancelled", "fail": "failed"},
            "running": {
                "pause": "paused",
                "cancel": "cancelled",
                "complete": "complete",
                "fail": "failed",
            },
            "paused": {"resume": "running", "cancel": "cancelled", "fail": "failed"},
        }
        next_state = transitions.get(attack.state, {}).get(request.action)
        if next_state is None:
            raise BrowserWorkflowError(
                f"cannot {request.action} an attack in {attack.state} state"
            )
        changes: dict[str, Any] = {"state": next_state, "error": request.error}
        if next_state == "running" and attack.started_at is None:
            changes["started_at"] = utc_now()
        if next_state in {"complete", "cancelled", "failed"}:
            changes["completed_at"] = utc_now()
        updated, _ = self.store.update_with_operation_event(
            BrowserAttack,
            attack.id,
            changes,
            expected_revision=request.expected_revision,
            operation_id=attack.id,
            operation_kind="browser_attack",
            engagement_id=attack.engagement_id,
            event_type=f"browser_attack.{next_state}",
            event_payload={},
            actor_id=request.actor_id,
        )
        return updated

    def add_attack_result(
        self, attack_id: str, request: AttackResultRequest, actor_id: str
    ) -> BrowserAttackResult:
        attack = self.store.get(BrowserAttack, attack_id)
        if attack.state != "running":
            raise BrowserWorkflowError("attack results require a running attack")
        if attack.request_count >= attack.max_requests:
            raise BrowserWorkflowError("attack request budget is exhausted")
        existing = next(
            (
                result
                for result in self.store.list_entities(
                    BrowserAttackResult,
                    engagement_id=attack.engagement_id,
                    limit=1_000,
                )
                if result.attack_id == attack.id and result.sequence == request.sequence
            ),
            None,
        )
        if existing is not None:
            return existing
        if request.exchange_id:
            self._owned(
                BrowserTrafficExchange, request.exchange_id, attack.engagement_id
            )
        result = BrowserAttackResult(
            engagement_id=attack.engagement_id,
            attack_id=attack.id,
            **request.model_dump(),
        )
        created = self._create(result, "browser_attack_result.created", actor_id)
        latest = self.store.get(BrowserAttack, attack.id)
        self.store.update_with_operation_event(
            BrowserAttack,
            latest.id,
            {
                "request_count": latest.request_count + 1,
                "error_count": latest.error_count + (1 if request.error else 0),
            },
            expected_revision=latest.revision,
            operation_id=latest.id,
            operation_kind="browser_attack",
            engagement_id=latest.engagement_id,
            event_type="browser_attack.progress",
            event_payload={"request_count": latest.request_count + 1},
            actor_id=actor_id,
        )
        return created

    def decode(self, request: DecoderRequest) -> dict[str, Any]:
        value = request.value
        operation = request.operation
        try:
            result: Any
            if operation == "url_encode":
                result = quote(value, safe="")
            elif operation == "url_decode":
                result = unquote(value)
            elif operation == "html_encode":
                result = html.escape(value, quote=True)
            elif operation == "html_decode":
                result = html.unescape(value)
            elif operation == "base64_encode":
                result = base64.b64encode(value.encode()).decode()
            elif operation == "base64_decode":
                result = base64.b64decode(value, validate=True).decode(
                    "utf-8", errors="replace"
                )
            elif operation == "hex_encode":
                result = value.encode().hex()
            elif operation == "hex_decode":
                result = bytes.fromhex(value).decode("utf-8", errors="replace")
            elif operation == "gzip_compress":
                result = base64.b64encode(gzip.compress(value.encode())).decode()
            elif operation == "gzip_decompress":
                result = gzip.decompress(base64.b64decode(value, validate=True)).decode(
                    "utf-8", errors="replace"
                )
            elif operation == "jwt_inspect":
                parts = value.split(".")
                if len(parts) != 3:
                    raise ValueError("JWTs require three dot-separated segments")
                result = {
                    "header": self._jwt_segment(parts[0]),
                    "payload": self._jwt_segment(parts[1]),
                    "signature_sha256": hashlib.sha256(parts[2].encode()).hexdigest(),
                    "verified": False,
                }
            else:
                result = hashlib.new(operation, value.encode()).hexdigest()
        except (ValueError, binascii.Error, OSError) as exc:
            raise BrowserWorkflowError(f"decoder operation failed: {exc}") from exc
        encoded = (
            json.dumps(result, ensure_ascii=False)
            if not isinstance(result, str)
            else result
        )
        if len(encoded.encode()) > MAX_UTILITY_INPUT:
            raise BrowserWorkflowError("decoder output exceeds the 1 MiB limit")
        return {
            "operation": operation,
            "result": result,
            "bytes": len(encoded.encode()),
        }

    def compare(self, request: CompareRequest) -> dict[str, Any]:
        left, right = request.left, request.right
        if request.mode == "json":
            try:
                left = json.dumps(
                    json.loads(left), indent=2, sort_keys=True, ensure_ascii=False
                )
                right = json.dumps(
                    json.loads(right), indent=2, sort_keys=True, ensure_ascii=False
                )
            except json.JSONDecodeError as exc:
                raise BrowserWorkflowError(
                    "JSON comparison requires valid JSON on both sides"
                ) from exc
        if request.mode == "bytes":
            try:
                left_bytes, right_bytes = (
                    base64.b64decode(left, validate=True),
                    base64.b64decode(right, validate=True),
                )
            except binascii.Error as exc:
                raise BrowserWorkflowError(
                    "byte comparison expects base64 input"
                ) from exc
            first_difference = next(
                (
                    index
                    for index, pair in enumerate(zip(left_bytes, right_bytes))
                    if pair[0] != pair[1]
                ),
                min(len(left_bytes), len(right_bytes))
                if len(left_bytes) != len(right_bytes)
                else None,
            )
            return {
                "mode": request.mode,
                "equal": left_bytes == right_bytes,
                "left_bytes": len(left_bytes),
                "right_bytes": len(right_bytes),
                "first_difference": first_difference,
            }
        diff = list(
            difflib.unified_diff(
                left.splitlines(),
                right.splitlines(),
                fromfile="left",
                tofile="right",
                lineterm="",
            )
        )[:10_000]
        return {
            "mode": request.mode,
            "equal": left == right,
            "similarity": difflib.SequenceMatcher(a=left, b=right).ratio(),
            "diff": diff,
            "truncated": len(diff) == 10_000,
        }

    def analyze_tokens(
        self, request: TokenAnalysisRequest, actor_id: str
    ) -> BrowserTokenAnalysis:
        session = self.store.get(BrowserSession, request.session_id)
        encoded_size = sum(len(sample.encode()) for sample in request.samples)
        if encoded_size > 8 * MAX_UTILITY_INPUT:
            raise BrowserWorkflowError("token samples exceed the 8 MiB analysis limit")
        for exchange_id in request.source_exchange_ids:
            self._owned(BrowserTrafficExchange, exchange_id, session.engagement_id)
        frequencies = Counter("".join(request.samples))
        total = sum(frequencies.values())
        entropy = (
            -sum(
                (count / total) * math.log2(count / total)
                for count in frequencies.values()
            )
            if total
            else 0.0
        )
        unique = len(set(request.samples))
        analysis = BrowserTokenAnalysis(
            engagement_id=session.engagement_id,
            session_id=session.id,
            name=request.name,
            sample_count=len(request.samples),
            token_length_min=min(map(len, request.samples)),
            token_length_max=max(map(len, request.samples)),
            unique_count=unique,
            collision_count=len(request.samples) - unique,
            shannon_bits_per_character=entropy,
            character_frequencies=dict(frequencies.most_common(256)),
            source_exchange_ids=request.source_exchange_ids,
            metadata={
                "analysis_version": "browser-sequencer-v1",
                "interpretation": "descriptive only; not a cryptographic certification",
            },
        )
        return self._create(analysis, "browser_token_analysis.created", actor_id)

    def promote_finding(
        self, engagement_id: str, request: FindingPromotionRequest, actor_id: str
    ) -> Finding:
        for evidence_id in request.evidence_ids:
            self._owned(Evidence, evidence_id, engagement_id)
        for node_id in request.site_node_ids:
            self._owned(BrowserSiteNode, node_id, engagement_id)
        for exchange_id in request.source_exchange_ids:
            self._owned(BrowserTrafficExchange, exchange_id, engagement_id)
        finding = Finding(
            engagement_id=engagement_id,
            title=request.title,
            description=request.description,
            status=FindingStatus.CANDIDATE,
            severity=request.severity,
            severity_rationale=request.severity_rationale,
            evidence_ids=request.evidence_ids,
            metadata={
                "source": "security-browser",
                "browser_site_node_ids": request.site_node_ids,
                "browser_exchange_ids": request.source_exchange_ids,
                "run_id": request.run_id,
            },
        )
        return self._create(finding, "browser_finding.candidate_created", actor_id)

    def import_har(
        self, engagement_id: str, request: HarImportRequest, actor_id: str
    ) -> dict[str, Any]:
        session = self._owned(BrowserSession, request.session_id, engagement_id)
        entries = (
            request.har.get("log", {}).get("entries")
            if isinstance(request.har.get("log"), dict)
            else None
        )
        if not isinstance(entries, list):
            raise BrowserWorkflowError("HAR import requires log.entries")
        if len(entries) > MAX_HAR_ENTRIES:
            raise BrowserWorkflowError("HAR import exceeds the 10000-entry limit")
        imported = 0
        skipped = 0
        for raw in entries:
            try:
                request_data = raw["request"]
                response_data = raw.get("response", {})
                url = str(request_data["url"])
                method = str(request_data.get("method") or "GET").upper()
                self.security._require_in_scope(
                    self.security._scope(engagement_id),
                    url,
                    "browser.har.import",
                    self._passive_risk(),
                )
                header_map = {
                    str(item.get("name", "")): str(item.get("value", ""))
                    for item in response_data.get("headers", [])[:200]
                }
                self.record_site_node(
                    SiteNodeRecordRequest(
                        session_id=session.id,
                        url=url,
                        method=method,
                        discovery_source="har",
                        status_code=response_data.get("status"),
                        content_type=next(
                            (
                                value
                                for name, value in header_map.items()
                                if name.lower() == "content-type"
                            ),
                            None,
                        ),
                        metadata={"har_imported": True, "bodies_omitted": True},
                    ),
                    actor_id,
                )
                imported += 1
            except (
                KeyError,
                TypeError,
                ValueError,
                BrowserWorkflowError,
            ):  # diagnostic-expected: malformed HAR entries are counted and skipped.
                skipped += 1
        return {
            "session_id": session.id,
            "entries": len(entries),
            "imported": imported,
            "skipped": skipped,
            "bodies_imported": 0,
            "redaction": "headers and bodies containing reusable secrets are not imported",
        }

    def export_har(self, engagement_id: str, session_id: str) -> dict[str, Any]:
        session = self._owned(BrowserSession, session_id, engagement_id)
        exchanges = [
            exchange
            for exchange in self.store.list_entities(
                BrowserTrafficExchange, engagement_id=engagement_id, limit=1_000
            )
            if exchange.session_id == session.id
        ]
        entries = []
        for exchange in exchanges:
            entries.append(
                {
                    "startedDateTime": exchange.started_at.isoformat(),
                    "time": exchange.duration_ms or 0,
                    "request": {
                        "method": exchange.method,
                        "url": exchange.url,
                        "httpVersion": exchange.protocol,
                        "headers": [
                            {"name": name, "value": value}
                            for name, value in exchange.request_headers.items()
                        ],
                        "queryString": [
                            {"name": name, "value": value}
                            for name, value in parse_qsl(
                                urlsplit(exchange.url).query, keep_blank_values=True
                            )
                        ],
                        "headersSize": -1,
                        "bodySize": exchange.request_bytes
                        if exchange.request_bytes is not None
                        else -1,
                    },
                    "response": {
                        "status": exchange.status_code or 0,
                        "statusText": "",
                        "httpVersion": exchange.protocol,
                        "headers": [
                            {"name": name, "value": value}
                            for name, value in exchange.response_headers.items()
                        ],
                        "content": {
                            "size": exchange.response_bytes or 0,
                            "mimeType": exchange.response_headers.get(
                                "content-type", "application/octet-stream"
                            ),
                            "comment": "Body omitted; see Nebula evidence artifacts when authorized.",
                        },
                        "redirectURL": "",
                        "headersSize": -1,
                        "bodySize": exchange.response_bytes
                        if exchange.response_bytes is not None
                        else -1,
                    },
                    "cache": {},
                    "timings": {
                        "send": 0,
                        "wait": exchange.duration_ms or 0,
                        "receive": 0,
                    },
                    "comment": f"Nebula exchange {exchange.id}; scope revision {exchange.scope_policy_revision}; redacted",
                }
            )
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "Nebula", "version": "3"},
                "comment": "Redacted HAR export; reusable secrets and bodies are omitted.",
                "entries": entries,
            }
        }

    @staticmethod
    def _normalized_site_url(value: str) -> str:
        parsed = urlsplit(value)
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path or "/",
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _redact_pairs(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        # Preserve order and duplicate names while redacting each reusable value.
        return [
            (name, redact_browser_headers({name: value})[name])
            for name, value in headers
        ]

    @staticmethod
    def _jwt_segment(value: str) -> Any:
        padded = value + "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))

    @staticmethod
    def _passive_risk():
        from .domain import RiskClass

        return RiskClass.PASSIVE

    @staticmethod
    def _active_risk():
        from .domain import RiskClass

        return RiskClass.ACTIVE_SCAN

    def _owned(self, model: type[Any], entity_id: str, engagement_id: str) -> Any:
        entity = self.store.get(model, entity_id)
        if getattr(entity, "engagement_id", None) != engagement_id:
            raise BrowserWorkflowError(
                "browser research object belongs to another Project"
            )
        return entity

    def _create(self, entity: Any, event_type: str, actor_id: str) -> Any:
        created, _ = self.store.create_with_operation_event(
            entity,
            operation_id=entity.id,
            operation_kind=entity.entity_kind,
            engagement_id=entity.engagement_id,
            event_type=event_type,
            event_payload={"entity_id": entity.id},
            actor_id=actor_id,
            idempotency_key=f"create:{entity.id}",
        )
        return created

    def _transition_intercept(
        self,
        item: BrowserInterceptItem,
        revision: int,
        state: Literal["interrupted", "expired"],
        actor_id: str,
        error: str,
    ) -> BrowserInterceptItem:
        updated, _ = self.store.update_with_operation_event(
            BrowserInterceptItem,
            item.id,
            {
                "state": state,
                "error": error,
                "decided_at": utc_now(),
                "decided_by": actor_id,
            },
            expected_revision=revision,
            operation_id=item.id,
            operation_kind="browser_intercept",
            engagement_id=item.engagement_id,
            event_type=f"browser_intercept.{state}",
            event_payload={"error": error},
            actor_id=actor_id,
        )
        return updated


__all__ = [
    "AttackCreateRequest",
    "AttackResultRequest",
    "AttackStateRequest",
    "BrowserResearchService",
    "BrowserResearchWorkspace",
    "CompareRequest",
    "DecoderRequest",
    "FindingPromotionRequest",
    "HarImportRequest",
    "InterceptCreateRequest",
    "InterceptDecisionRequest",
    "RepeaterResultRequest",
    "RepeaterTabCreateRequest",
    "RepeaterTabUpdateRequest",
    "SiteEdgeRecordRequest",
    "SiteNodeRecordRequest",
    "TokenAnalysisRequest",
]
