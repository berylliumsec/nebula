"""One selectable Core MCP surface for the isolated agent security browser."""

from __future__ import annotations

import asyncio
import base64
import html
import json
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import quote, unquote, urljoin

from .agent_security import AgentSecuritySessions
from .browser_engine import BrowserEngineAction
from .browser_research import (
    AttackCreateRequest,
    AttackResultRequest,
    AttackStateRequest,
    BrowserResearchService,
    CrawlCreateRequest,
    CrawlStateRequest,
    FindingPromotionRequest,
    InterceptDecisionRequest,
    RepeaterResultRequest,
    RepeaterStateRequest,
    RepeaterTabCreateRequest,
    SiteNodeRecordRequest,
    TokenAnalysisRequest,
)
from .browser_security import BrowserCaptureSettingsRequest, BrowserSecurityService
from .browserd import BrowserdSecurityRequest
from .domain import (
    BrowserAttack,
    BrowserCrawlJob,
    BrowserInterceptItem,
    BrowserSession,
    BrowserSessionStatus,
    BrowserTrafficExchange,
    Evidence,
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    RiskClass,
    utc_now,
)
from .storage import NebulaStore, NotFoundError
from .security_upstream import parse_upstream_settings


PROFILE_ID = "nebula-security-tools-v1"
PROFILE_NAME = "browser-security-testing-tools"
_ACTOR = "security-tools-mcp"
_CURATED: dict[str, list[str]] = {
    "booleans": ["true", "false", "1", "0", "null"],
    "boundary_numbers": ["-1", "0", "1", "2147483647", "2147483648"],
    "path_boundaries": [".", "..", "../", "%2e%2e%2f"],
    "header_boundaries": ["", "0", "null", "undefined"],
}


def _object(properties: dict[str, Any], required: list[str] = []) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_URL = {"type": "string", "minLength": 1, "maxLength": 16_384}
_HEADERS = {
    "type": "object",
    "additionalProperties": {"type": "string"},
    "maxProperties": 200,
}
_TOOLS: tuple[tuple[str, str, dict[str, Any], bool], ...] = (
    (
        "status",
        "Read the headless security session, intercept queue, traffic and attack status.",
        _object({}),
        True,
    ),
    (
        "navigate",
        "Open an in-scope URL in the headless security identity. This does not touch the visible browser.",
        _object({"url": _URL}, ["url"]),
        False,
    ),
    (
        "intercept",
        "Enable or disable flow interception in the headless security proxy.",
        _object({"enabled": {"type": "boolean"}}, ["enabled"]),
        False,
    ),
    (
        "upstream_proxy",
        "Route new headless security connections through an HTTP(S) or SOCKS5 proxy using an optional protected credential reference.",
        _object(
            {
                "enabled": {"type": "boolean"},
                "url": _URL,
                "credential_ref": _STRING,
            },
            ["enabled"],
        ),
        False,
    ),
    (
        "intercept_decide",
        "Forward or drop one paused headless request.",
        _object(
            {
                "intercept_id": _STRING,
                "decision": {"enum": ["forward", "drop"]},
                "method": _STRING,
                "url": _URL,
            },
            ["intercept_id", "decision"],
        ),
        False,
    ),
    (
        "repeater",
        "Send one scoped HTTP request using the headless identity and save its Repeater result.",
        _object(
            {"method": _STRING, "url": _URL, "headers": _HEADERS, "body": _STRING},
            ["method", "url"],
        ),
        False,
    ),
    (
        "intruder",
        "Queue a bounded headless Intruder attack with sniper, battering ram, pitchfork or cluster bomb payloads.",
        _object(
            {
                "strategy": {
                    "enum": ["sniper", "battering_ram", "pitchfork", "cluster_bomb"]
                },
                "method": _STRING,
                "url_template": _URL,
                "headers_template": {"type": "array"},
                "body_template": _STRING,
                "positions": {"type": "array", "items": _STRING},
                "payload_sets": {"type": "array"},
                "transforms": {"type": "array", "items": _STRING},
                "max_requests": {"type": "integer", "minimum": 1, "maximum": 100000},
                "requests_per_second": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "maximum": 100,
                },
            },
            ["strategy", "method", "url_template", "positions", "payload_sets"],
        ),
        False,
    ),
    (
        "crawl",
        "Queue a bounded scoped crawl in the headless security identity.",
        _object(
            {
                "start_url": _URL,
                "max_depth": {"type": "integer", "minimum": 0, "maximum": 10},
                "max_requests": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["start_url"],
        ),
        False,
    ),
    (
        "traffic",
        "Read recent redacted HTTP exchanges from the headless security session.",
        _object({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
        True,
    ),
    (
        "har_export",
        "Read a bounded redacted HAR export of the headless security session.",
        _object({"limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
        True,
    ),
    (
        "token_analysis",
        "Save descriptive analysis of token samples from the headless security session.",
        _object(
            {
                "name": _STRING,
                "samples": {
                    "type": "array",
                    "items": _STRING,
                    "minItems": 1,
                    "maxItems": 1000,
                },
                "source_exchange_ids": {
                    "type": "array",
                    "items": _STRING,
                    "maxItems": 100,
                },
            },
            ["samples"],
        ),
        False,
    ),
    (
        "evidence_from_exchange",
        "Save a redacted evidence reference to one captured headless exchange.",
        _object(
            {"exchange_id": _STRING, "title": _STRING, "description": _STRING},
            ["exchange_id", "title"],
        ),
        False,
    ),
    (
        "finding_candidate",
        "Create a candidate finding from saved evidence and captured headless exchanges.",
        _object(
            {
                "title": _STRING,
                "description": _STRING,
                "severity": {"enum": ["info", "low", "medium", "high", "critical"]},
                "severity_rationale": _STRING,
                "evidence_ids": {
                    "type": "array",
                    "items": _STRING,
                    "minItems": 1,
                    "maxItems": 100,
                },
                "source_exchange_ids": {
                    "type": "array",
                    "items": _STRING,
                    "maxItems": 100,
                },
            },
            ["title", "evidence_ids"],
        ),
        False,
    ),
    (
        "stop",
        "Stop the headless security identity and all queued security work for this Project.",
        _object({}),
        False,
    ),
)


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"a", "link", "iframe"}:
            name = "src" if tag == "iframe" else "href"
            self.links.extend(value for key, value in attrs if key == name and value)


def _payload_values(source: dict[str, Any]) -> list[str]:
    if source.get("kind") == "curated":
        return list(_CURATED[str(source.get("name"))])
    return list(source["values"])


def _transform(value: str, transforms: list[str]) -> str:
    for transform in transforms:
        if transform == "url_encode":
            value = quote(value, safe="")
        elif transform == "url_decode":
            value = unquote(value)
        elif transform == "base64_encode":
            value = base64.b64encode(value.encode()).decode()
        elif transform == "base64_decode":
            value = base64.b64decode(value).decode(errors="replace")
        elif transform == "hex_encode":
            value = value.encode().hex()
        elif transform == "hex_decode":
            value = bytes.fromhex(value).decode(errors="replace")
        elif transform == "html_encode":
            value = html.escape(value, quote=True)
        elif transform == "html_decode":
            value = html.unescape(value)
        elif transform == "lowercase":
            value = value.lower()
        elif transform == "uppercase":
            value = value.upper()
    return value


def _attack_payloads(attack: BrowserAttack, sequence: int) -> dict[str, str] | None:
    sets = [_payload_values(source) for source in attack.payload_sets]
    if not sets or any(not values for values in sets):
        return None
    result: dict[str, str] = {}
    if attack.strategy == "battering_ram":
        if sequence >= len(sets[0]):
            return None
        result = {position: sets[0][sequence] for position in attack.positions}
    elif attack.strategy == "sniper":
        index, offset = divmod(sequence, len(sets[0]))
        if index >= len(attack.positions):
            return None
        result = dict.fromkeys(attack.positions, "")
        result[attack.positions[index]] = sets[0][offset]
    elif attack.strategy == "pitchfork":
        if sequence >= min(map(len, sets)):
            return None
        result = {
            position: sets[index][sequence]
            for index, position in enumerate(attack.positions)
        }
    else:
        remaining = sequence
        for index in range(len(sets) - 1, -1, -1):
            remaining, offset = divmod(remaining, len(sets[index]))
            result[attack.positions[index]] = sets[index][offset]
        if remaining:
            return None
    return {
        name: _transform(value, attack.transforms) for name, value in result.items()
    }


def _substitute(template: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        template = template.replace(f"§{name}§", value)
    return template


class SecurityToolsRuntime:
    """Project-bound MCP dispatch; only Core owns execution and durable state."""

    def __init__(self, store: NebulaStore, sessions: AgentSecuritySessions) -> None:
        self.store = store
        self.sessions = sessions
        self.security = BrowserSecurityService(store)
        self.research = BrowserResearchService(store, self.security)
        self._tasks: set[asyncio.Task[None]] = set()
        self._project_tasks: dict[str, set[asyncio.Task[None]]] = {}

    @staticmethod
    def capabilities() -> McpCapabilitySnapshot:
        return McpCapabilitySnapshot(
            protocol_version="2025-06-18",
            checked_at=utc_now(),
            instructions="Security Tools operate only on a separate Core-owned headless browser. The visible desktop browser is not captured or intercepted.",
            tools=[
                McpToolSnapshot(
                    name=name,
                    description=description,
                    input_schema=schema,
                    read_only=read_only,
                    destructive=False,
                    idempotent=read_only,
                    open_world=False,
                    credentialed=False,
                    annotations_complete=True,
                )
                for name, description, schema, read_only in _TOOLS
            ],
        )

    def register_profile(self) -> McpServerProfile:
        try:
            profile = self.store.get(McpServerProfile, PROFILE_ID)
        except NotFoundError:
            return self.store.create(
                McpServerProfile(
                    id=PROFILE_ID,
                    name=PROFILE_NAME,
                    description="Headless Project security testing: interception, Repeater, Intruder and crawl.",
                    transport=McpTransport.BUILTIN,
                    enabled=True,
                    default_approval=McpApprovalMode.RISK_BASED,
                    capabilities=self.capabilities(),
                    metadata={"built_in": "agent_security_v1"},
                )
            )
        if (
            profile.transport != McpTransport.BUILTIN
            or profile.metadata.get("built_in") != "agent_security_v1"
        ):
            raise ValueError("Security Tools profile ID is occupied by another server")
        fresh = self.capabilities()
        definition_changed = [
            tool.model_dump(exclude={"checked_at"})
            for tool in profile.capabilities.tools
        ] != [tool.model_dump(exclude={"checked_at"}) for tool in fresh.tools]
        if profile.name != PROFILE_NAME or definition_changed:
            profile = self.store.update(
                McpServerProfile,
                PROFILE_ID,
                {"name": PROFILE_NAME, "capabilities": fresh.model_dump(mode="json")},
                expected_revision=profile.revision,
            )
        return profile

    @staticmethod
    def _result(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
            ],
            "structuredContent": value,
            "isError": False,
        }

    def _active(self, project_id: str) -> BrowserSession | None:
        return next(
            (
                session
                for session in self.store.find_entities(
                    BrowserSession,
                    {"runtime_kind": "agent_security"},
                    engagement_id=project_id,
                    newest_first=True,
                )
                if session.status == BrowserSessionStatus.ACTIVE
            ),
            None,
        )

    async def call_tool(
        self, project_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if name == "status":
            session = self._active(project_id)
            security = self.security.workspace(project_id)
            research = self.research.workspace(project_id)

            def owned(item: Any) -> bool:
                return session is not None and item.session_id == session.id

            return self._result(
                {
                    "session_id": session.id if session else None,
                    "state": session.status.value if session else "not_started",
                    "interception_enabled": session.interception_enabled
                    if session
                    else False,
                    "upstream_proxy_configured": bool(
                        session and session.upstream_proxy_enabled
                    ),
                    "traffic_count": sum(map(owned, security.traffic)),
                    "paused_intercepts": [
                        item.model_dump(mode="json")
                        for item in research.intercepts
                        if owned(item) and item.state == "paused"
                    ],
                    "attacks": [
                        {
                            "id": item.id,
                            "state": item.state,
                            "request_count": item.request_count,
                            "max_requests": item.max_requests,
                        }
                        for item in research.attacks
                        if owned(item)
                    ],
                    "crawl_jobs": [
                        {
                            "id": item.id,
                            "state": item.state,
                            "requests_completed": item.requests_completed,
                        }
                        for item in research.crawl_jobs
                        if owned(item)
                    ],
                }
            )
        if name == "stop":
            stopped = await self.stop(project_id, reason="Security Tools MCP stop")
            return self._result({"stopped_sessions": stopped})
        if name == "intercept_decide":
            item = self.store.get(BrowserInterceptItem, str(arguments["intercept_id"]))
            active = self._active(project_id)
            if (
                active is None
                or item.session_id != active.id
                or item.engagement_id != project_id
            ):
                raise ValueError(
                    "intercept is not in the active Project security session"
                )
            updated = self.research.decide_intercept(
                item.id,
                InterceptDecisionRequest(
                    expected_revision=item.revision,
                    decision=arguments["decision"],
                    operator_id=_ACTOR,
                    method=arguments.get("method"),
                    url=arguments.get("url"),
                ),
            )
            return self._result({"intercept_id": updated.id, "state": updated.state})
        if name in {
            "traffic",
            "har_export",
            "token_analysis",
            "evidence_from_exchange",
            "finding_candidate",
        }:
            session = self._active(project_id)
            if session is None:
                raise ValueError(
                    "Start the headless security session before reading or saving research"
                )
            if name == "traffic":
                limit = int(arguments.get("limit", 30))
                if not 1 <= limit <= 100:
                    raise ValueError("traffic limit must be between 1 and 100")
                exchanges = self.store.find_entities(
                    BrowserTrafficExchange,
                    {"session_id": session.id},
                    engagement_id=project_id,
                    newest_first=True,
                    limit=limit,
                )
                return self._result(
                    {
                        "session_id": session.id,
                        "exchanges": [
                            {
                                "id": item.id,
                                "method": item.method,
                                "url": item.url,
                                "status_code": item.status_code,
                                "protocol": item.protocol,
                                "request_headers": item.request_headers,
                                "response_headers": item.response_headers,
                                "request_bytes": item.request_bytes,
                                "response_bytes": item.response_bytes,
                                "blocked": item.blocked,
                                "error": item.error,
                            }
                            for item in exchanges
                        ],
                    }
                )
            if name == "har_export":
                limit = int(arguments.get("limit", 100))
                if not 1 <= limit <= 500:
                    raise ValueError("HAR limit must be between 1 and 500")
                har = self.research.export_har(project_id, session.id, limit=limit)
                truncated = har.pop("truncated")
                return self._result(
                    {
                        "har": har,
                        "returned_entries": len(har["log"]["entries"]),
                        "truncated": truncated,
                    }
                )
            if name == "token_analysis":
                self._require_headless_exchanges(
                    session, arguments.get("source_exchange_ids", [])
                )
                analysis = self.research.analyze_tokens(
                    TokenAnalysisRequest.model_validate(
                        {
                            **arguments,
                            "session_id": session.id,
                        }
                    ),
                    _ACTOR,
                )
                return self._result(
                    {
                        "analysis_id": analysis.id,
                        "sample_count": analysis.sample_count,
                        "unique_count": analysis.unique_count,
                        "shannon_bits_per_character": analysis.shannon_bits_per_character,
                    }
                )
            if name == "evidence_from_exchange":
                exchange = self._require_headless_exchanges(
                    session, [arguments["exchange_id"]]
                )[0]
                evidence = Evidence(
                    engagement_id=project_id,
                    evidence_type="browser_traffic",
                    title=str(arguments["title"])[:500],
                    description=str(arguments.get("description", ""))[:20_000],
                    captured_by=_ACTOR,
                    source_version="agent-security-v1",
                    metadata={
                        "browser_session_id": session.id,
                        "browser_exchange_id": exchange.id,
                        "redacted": True,
                    },
                )
                saved, _ = self.store.create_with_operation_event(
                    evidence,
                    operation_id=evidence.id,
                    operation_kind="evidence",
                    engagement_id=project_id,
                    event_type="agent_security.evidence_saved",
                    event_payload={"exchange_id": exchange.id},
                    actor_id=_ACTOR,
                    idempotency_key=f"create:{evidence.id}",
                )
                return self._result(
                    {"evidence_id": saved.id, "exchange_id": exchange.id}
                )
            self._require_headless_exchanges(
                session, arguments.get("source_exchange_ids", [])
            )
            finding = self.research.promote_finding(
                project_id, FindingPromotionRequest.model_validate(arguments), _ACTOR
            )
            return self._result(
                {"finding_id": finding.id, "status": finding.status.value}
            )
        session = await self.sessions.ensure(project_id)
        if name == "navigate":
            url = str(arguments["url"])
            self.security._require_in_scope(
                self.security._scope(project_id),
                url,
                "agent_security.navigate",
                RiskClass.ACTIVE_SCAN,
                native_scope_authority=True,
            )
            adapter = await self.sessions.host.adapter()
            receipt = await adapter.execute(
                BrowserEngineAction(
                    action_token=f"security-navigate-{__import__('uuid').uuid4()}",
                    assessment_id=session.id,
                    session_id=session.id,
                    identity_id=session.identity_id,
                    tab_id=session.active_tab_id or "",
                    kind="navigate",
                    arguments={"url": url},
                )
            )
            return self._result(
                {
                    "session_id": session.id,
                    "state": receipt.state,
                    "url": receipt.page_url,
                    "failure_code": receipt.failure_code,
                }
            )
        if name == "intercept":
            capture_session = self.security.update_capture_settings(
                session.id,
                BrowserCaptureSettingsRequest(
                    expected_revision=session.revision,
                    capture_mode=session.capture_mode,
                    proxy_enabled=True,
                    trust_acknowledged=True,
                    interception_enabled=bool(arguments["enabled"]),
                    upstream_proxy_enabled=session.upstream_proxy_enabled,
                    upstream_proxy_url=session.upstream_proxy_url,
                    upstream_proxy_credential_ref=session.upstream_proxy_credential_ref,
                ),
                _ACTOR,
            )
            return self._result(
                {
                    "session_id": session.id,
                    "enabled": capture_session.interception_enabled,
                }
            )
        if name == "upstream_proxy":
            if self._project_tasks.get(project_id):
                raise ValueError(
                    "Stop active security work before changing the upstream proxy"
                )
            paused = self.store.find_entities(
                BrowserInterceptItem,
                {"session_id": session.id, "state": "paused"},
                engagement_id=project_id,
                limit=1,
            )
            if paused:
                raise ValueError(
                    "Decide paused intercepts before changing the upstream proxy"
                )
            enabled = bool(arguments["enabled"])
            proxy_url = str(arguments.get("url") or "") if enabled else None
            reference = (
                str(arguments["credential_ref"])
                if enabled and arguments.get("credential_ref")
                else None
            )
            if enabled:
                if not proxy_url:
                    raise ValueError("An enabled upstream proxy requires a URL")
                credentials = self.sessions.host.credentials
                credential = (
                    credentials.resolve(reference).get_secret_value()
                    if reference and credentials is not None
                    else None
                )
                if reference and credential is None:
                    raise ValueError(
                        "The protected upstream proxy credential is unavailable"
                    )
                parse_upstream_settings(proxy_url, credential)
            self.security.update_capture_settings(
                session.id,
                BrowserCaptureSettingsRequest(
                    expected_revision=session.revision,
                    capture_mode=session.capture_mode,
                    proxy_enabled=True,
                    trust_acknowledged=True,
                    interception_enabled=session.interception_enabled,
                    upstream_proxy_enabled=enabled,
                    upstream_proxy_url=proxy_url,
                    upstream_proxy_credential_ref=reference,
                ),
                _ACTOR,
            )
            try:
                await self.sessions.host.close_identity(session.identity_id)
                await self.sessions.ensure(project_id)
            except Exception:
                await self.sessions.stop(
                    project_id, reason="Upstream proxy reconfiguration failed"
                )
                raise
            return self._result(
                {"session_id": session.id, "upstream_proxy_configured": enabled}
            )
        if name == "repeater":
            tab = self.research.create_repeater_tab(
                RepeaterTabCreateRequest(
                    session_id=session.id,
                    identity_id=session.identity_id,
                    method=arguments["method"],
                    url=arguments["url"],
                    headers=list(arguments.get("headers", {}).items()),
                    body_template=arguments.get("body", ""),
                ),
                _ACTOR,
            )
            tab = self.research.transition_repeater_tab(
                tab.id,
                RepeaterStateRequest(
                    expected_revision=tab.revision,
                    action="queue",
                    actor_id=_ACTOR,
                ),
            )
            tab = self.research.transition_repeater_tab(
                tab.id,
                RepeaterStateRequest(
                    expected_revision=tab.revision,
                    action="start",
                    actor_id=_ACTOR,
                ),
            )
            try:
                response = await self.sessions.request(
                    project_id,
                    BrowserdSecurityRequest(
                        identity_id=session.identity_id,
                        method=tab.method,
                        url=tab.url,
                        headers=dict(arguments.get("headers", {})),
                        body=tab.body_template,
                    ),
                )
                result = RepeaterResultRequest(
                    expected_revision=tab.revision,
                    actor_id=_ACTOR,
                    status_code=response.status_code,
                    response_headers=list(response.headers.items()),
                    response_bytes=response.body_bytes,
                )
            except Exception as exc:
                result = RepeaterResultRequest(
                    expected_revision=tab.revision,
                    actor_id=_ACTOR,
                    error=f"{type(exc).__name__}: {str(exc)[:3000]}",
                )
            repeater_result = self.research.record_repeater_result(tab.id, result)
            return self._result(
                {
                    "repeater_id": tab.id,
                    "result_id": repeater_result.id,
                    "status_code": repeater_result.status_code,
                    "error": repeater_result.error,
                }
            )
        if name == "intruder":
            attack_request = AttackCreateRequest.model_validate(
                {
                    **arguments,
                    "session_id": session.id,
                    "identity_id": session.identity_id,
                }
            )
            attack = self.research.create_attack(attack_request, _ACTOR)
            attack = self.research.transition_attack(
                attack.id,
                AttackStateRequest(
                    expected_revision=attack.revision,
                    action="queue",
                    actor_id=_ACTOR,
                ),
            )
            self._start_task(project_id, self._run_attack(project_id, attack.id))
            return self._result(
                {
                    "attack_id": attack.id,
                    "state": "queued",
                    "max_requests": attack.max_requests,
                }
            )
        if name == "crawl":
            crawl_request = CrawlCreateRequest.model_validate(
                {
                    **arguments,
                    "session_id": session.id,
                    "identity_id": session.identity_id,
                }
            )
            job = self.research.create_crawl(crawl_request, _ACTOR)
            job = self.research.transition_crawl(
                job.id,
                CrawlStateRequest(
                    expected_revision=job.revision,
                    action="queue",
                    actor_id=_ACTOR,
                ),
            )
            self._start_task(project_id, self._run_crawl(project_id, job.id))
            return self._result(
                {
                    "crawl_id": job.id,
                    "state": "queued",
                    "max_requests": job.max_requests,
                }
            )
        raise ValueError("unknown Security Tools operation")

    def _require_headless_exchanges(
        self, session: BrowserSession, exchange_ids: list[str]
    ) -> list[BrowserTrafficExchange]:
        exchanges = []
        for exchange_id in exchange_ids:
            exchange = self.store.get(BrowserTrafficExchange, exchange_id)
            if (
                exchange.engagement_id != session.engagement_id
                or exchange.session_id != session.id
            ):
                raise ValueError(
                    "research exchange belongs to another security session"
                )
            exchanges.append(exchange)
        return exchanges

    def _start_task(self, project_id: str, work: Any) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        project_tasks = self._project_tasks.setdefault(project_id, set())
        project_tasks.add(task)

        def forget(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            project_tasks.discard(completed)
            if not project_tasks:
                self._project_tasks.pop(project_id, None)
            # An unexpected worker failure must not become an unobserved task.
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(forget)

    async def stop(self, project_id: str, *, reason: str) -> int:
        tasks = list(self._project_tasks.get(project_id, ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return await self.sessions.stop(project_id, reason=reason)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_attack(self, project_id: str, attack_id: str) -> None:
        attack = self.store.get(BrowserAttack, attack_id)
        try:
            attack = self.research.transition_attack(
                attack.id,
                AttackStateRequest(
                    expected_revision=attack.revision,
                    action="start",
                    actor_id=_ACTOR,
                ),
            )
            while (
                attack.state == "running" and attack.request_count < attack.max_requests
            ):
                if self._active(project_id) is None:
                    raise ValueError("the security session stopped")
                values = _attack_payloads(attack, attack.request_count)
                if values is None:
                    self.research.transition_attack(
                        attack.id,
                        AttackStateRequest(
                            expected_revision=attack.revision,
                            action="complete",
                            actor_id=_ACTOR,
                        ),
                    )
                    return
                sequence = attack.request_count
                url = _substitute(attack.url_template, values)
                headers = {
                    name: _substitute(value, values)
                    for name, value in attack.headers_template
                }
                try:
                    response = await self.sessions.request(
                        project_id,
                        BrowserdSecurityRequest(
                            identity_id=attack.identity_id,
                            method=attack.method,
                            url=url,
                            headers=headers,
                            body=_substitute(attack.body_template, values),
                        ),
                    )
                    result = AttackResultRequest(
                        sequence=sequence,
                        payloads=[values[name] for name in attack.positions],
                        status_code=response.status_code,
                        response_bytes=response.body_bytes,
                    )
                except Exception as exc:
                    if self._active(project_id) is None:
                        raise
                    result = AttackResultRequest(
                        sequence=sequence,
                        payloads=[values[name] for name in attack.positions],
                        error=f"{type(exc).__name__}: {str(exc)[:3000]}",
                    )
                self.research.add_attack_result(attack.id, result, _ACTOR)
                attack = self.store.get(BrowserAttack, attack.id)
                if attack.state == "running":
                    await asyncio.sleep(1 / attack.requests_per_second)
        except (Exception, asyncio.CancelledError) as exc:
            latest = self.store.get(BrowserAttack, attack_id)
            if latest.state in {"queued", "running", "paused"}:
                action: Literal["cancel", "fail"] = (
                    "cancel"
                    if isinstance(exc, asyncio.CancelledError)
                    or self._active(project_id) is None
                    else "fail"
                )
                self.research.transition_attack(
                    latest.id,
                    AttackStateRequest(
                        expected_revision=latest.revision,
                        action=action,
                        actor_id=_ACTOR,
                        error=str(exc)[:3000] or "Security session stopped",
                    ),
                )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _run_crawl(self, project_id: str, crawl_id: str) -> None:
        job = self.store.get(BrowserCrawlJob, crawl_id)
        try:
            job = self.research.transition_crawl(
                job.id,
                CrawlStateRequest(
                    expected_revision=job.revision,
                    action="start",
                    actor_id=_ACTOR,
                ),
            )
            while (
                job.state == "running"
                and job.frontier
                and job.requests_completed < job.max_requests
            ):
                if self._active(project_id) is None:
                    raise ValueError("the security session stopped")
                url, depth = job.frontier[0]
                response = await self.sessions.request(
                    project_id,
                    BrowserdSecurityRequest(
                        identity_id=job.identity_id,
                        method="GET",
                        url=url,
                        max_body_bytes=job.max_body_bytes,
                    ),
                )
                self.research.record_site_node(
                    SiteNodeRecordRequest(
                        session_id=job.session_id,
                        url=url,
                        discovery_source="crawl",
                        status_code=response.status_code,
                        content_type=response.headers.get("content-type"),
                    ),
                    _ACTOR,
                )
                visited = list(dict.fromkeys([*job.visited_urls, url]))[-10_000:]
                frontier = job.frontier[1:]
                if depth < job.max_depth and "html" in response.headers.get(
                    "content-type", ""
                ):
                    parser = _Links()
                    parser.feed(
                        base64.b64decode(response.body_base64).decode(errors="replace")
                    )
                    for link in parser.links[:1_000]:
                        target = urljoin(url, link)
                        if target in visited or any(
                            queued == target for queued, _ in frontier
                        ):
                            continue
                        try:
                            self.security._require_in_scope(
                                self.security._scope(project_id),
                                target,
                                "agent_security.crawl",
                                RiskClass.ACTIVE_SCAN,
                                native_scope_authority=True,
                            )
                        except Exception:
                            continue
                        frontier.append((target, depth + 1))
                        if len(frontier) >= job.max_requests:
                            break
                complete = (
                    not frontier or job.requests_completed + 1 >= job.max_requests
                )
                job = self.research.transition_crawl(
                    job.id,
                    CrawlStateRequest(
                        expected_revision=job.revision,
                        action="complete" if complete else "progress",
                        actor_id=_ACTOR,
                        requests_completed=job.requests_completed + 1,
                        nodes_discovered=len(visited),
                        checkpoint=job.checkpoint + 1,
                        frontier=frontier,
                        visited_urls=visited,
                    ),
                )
        except (Exception, asyncio.CancelledError) as exc:
            latest = self.store.get(BrowserCrawlJob, crawl_id)
            if latest.state in {"queued", "running", "paused"}:
                action: Literal["cancel", "fail"] = (
                    "cancel"
                    if isinstance(exc, asyncio.CancelledError)
                    or self._active(project_id) is None
                    else "fail"
                )
                self.research.transition_crawl(
                    latest.id,
                    CrawlStateRequest(
                        expected_revision=latest.revision,
                        action=action,
                        actor_id=_ACTOR,
                        error=str(exc)[:3000] or "Security session stopped",
                    ),
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
