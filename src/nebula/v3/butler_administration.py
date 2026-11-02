"""Closed, bounded Core observations and ordinary guide metadata for Butler."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import func, or_, select

from .action_registry import ActionRegistry
from .butler_data import ButlerData, _ID, _schema
from .butler_inspection import _CHUNK, _part
from .domain import HarnessSession, ResourceKind, RiskClass, SearchScope
from .database import EntityRow
from .guides import UNNAMED_OPERATOR, starter_files
from .operators import OperatorProfileService
from .search import FederatedSearch
from .tools import AnalysisTool, GuidedToolError, IdempotencyBehavior, ToolSpec


# IDs, names and step counts are checked against the actual UI guide catalog.
GUIDES = (
    ("assistant-runtime", "Choose who answers: a model or a coding harness", 2),
    ("bring-context", "Bring files, pages and selections into the chat", 2),
    ("side-terminal", "Run the assistant’s commands beside the chat", 2),
    ("command-approvals", "Decide what the assistant may run", 2),
    ("queue-and-steer", "Keep talking while the assistant works", 1),
    ("branch-and-find", "Branch, fork and find earlier messages", 2),
    ("conversation-goals", "Let the assistant work toward a goal", 2),
    ("mcp-tools", "Give the assistant MCP tools", 2),
    ("project-knowledge", "Answer from your project’s documents", 2),
    ("tool-assistance", "Get suggested next steps after each command", 2),
    ("missions-and-catch-up", "Hand work to a mission and catch up later", 3),
    ("lifecycle-hooks", "Run your own script on every chat turn", 5),
    ("agents-md", "Give the assistant standing project instructions", 3),
    ("project-skills", "Call a project skill with $", 3),
    ("provider-env-key", "Use a provider key from an environment variable", 2),
    ("lan-pairing", "Pair your phone over LAN", 2),
    ("shortcuts", "Shortcuts and the command palette", 5),
)
_SYSTEM = {
    "health": "/api/v1/health",
    "diagnostic_errors": "/api/v1/diagnostics/errors?limit=50",
    "diagnostic_files": "/api/v1/diagnostics/files",
    "diagnostic_settings": "/api/v1/diagnostics/settings",
    "runtime_usage": "/api/v1/runtime-usage",
    "setup_status": "/api/v1/setup/status",
    "automation_runtime": "/api/v1/automation/runtime?engagement_id={project}",
    "desktop_browser": "/api/v1/desktop-browser-mode",
    "source_control": "/api/v1/engagements/{project}/workspace/source-control",
    "execution_capabilities": "/api/v1/engagements/{project}/execution-capabilities",
    "terminal_public_ip": "/api/v1/engagements/{project}/container-terminal/public-ip",
    "issues": "/api/v1/engagements/{project}/butler/issues",
}


class ButlerAdministration(ButlerData):
    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        if self.core_api is None:
            raise ValueError("reconnect Butler to Core before inspecting product state")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.core_api.app),
            base_url="http://nebula-core",
            headers={"Authorization": f"Bearer {self.core_api.token}"},
            timeout=30,
        ) as client:
            response = await client.request(method, path, json=body)
        if not response.is_success:
            raise GuidedToolError(
                "Core did not confirm the product operation",
                code="product_state_unavailable",
                category="invalid_arguments",
                problem=f"Core returned HTTP {response.status_code} for the requested observation or change.",
                next_action="Read the current Core state and revision before retrying; preserve unsaved operator edits.",
                before_execution=method == "GET",
            )
        return response.json() if response.content else None

    async def system(self, arguments: dict[str, Any]) -> dict[str, Any]:
        kind = arguments["kind"]
        if kind == "source_diff":
            path = (
                f"/api/v1/engagements/{quote(self.project_id, safe='')}/workspace/source-control/diff?"
                + urlencode(
                    {
                        "path": arguments["path"],
                        "staged": str(arguments.get("staged", False)).lower(),
                    }
                )
            )
        elif kind == "side_chats":
            chat = self._chat(arguments["session_id"])
            path = f"/api/v1/chat/sessions/{quote(chat.id, safe='')}/side-chats"
        elif kind == "harness_sessions":
            private_sessions = select(
                EntityRow.payload["harness_session_id"].as_string()
            ).where(
                EntityRow.kind == "chat_sessions",
                EntityRow.payload["harness_session_id"].as_string().is_not(None),
                or_(
                    *[
                        func.coalesce(
                            EntityRow.payload["metadata"][key].as_boolean(), False
                        ).is_(True)
                        for key in (
                            "control_assistant",
                            "side_chat",
                            "temporary_assistant",
                        )
                    ]
                ),
            )
            with self.store.database.session() as database:
                rows = [
                    HarnessSession.model_validate(row.payload)
                    for row in database.scalars(
                        select(EntityRow)
                        .where(
                            EntityRow.kind == HarnessSession.entity_kind,
                            EntityRow.engagement_id == self.project_id,
                            EntityRow.id.not_in(private_sessions),
                        )
                        .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
                        .offset(arguments.get("page_offset", 0))
                        .limit(21)
                    )
                ]
            # Only the public roster: no account tokens, working directories,
            # private assistant sessions or harness transport metadata.
            roster = {
                "sessions": [
                    {
                        "id": row.id,
                        "revision": row.revision,
                        "harness_profile_id": row.harness_profile_id,
                        "display_name": row.display_name,
                        "model": row.model,
                        "status": row.status.value,
                    }
                    for row in rows[:20]
                ],
                "next_offset": arguments.get("page_offset", 0) + 20
                if len(rows) > 20
                else None,
            }
            return {"kind": kind, "read_back": True, **_part(roster, arguments)}
        elif kind == "diagnostic_incidents":
            # Resolve only Core's own recent records. Model-supplied diagnostic
            # records could otherwise invent an incident or an action target.
            path = "/api/v1/diagnostics/incidents/resolve"
        else:
            path = _SYSTEM[kind].replace("{project}", quote(self.project_id, safe=""))
        value = await self._request(
            "POST" if kind == "diagnostic_incidents" else "GET",
            path,
            {"records": []} if kind == "diagnostic_incidents" else None,
        )
        if kind == "side_chats":
            # Existence and display labels suffice for navigation; side-pane
            # transcripts are private and remain on the requesting browser.
            value = [
                {key: row.get(key) for key in ("id", "title", "revision", "status")}
                for row in value[:50]
            ]
        return {"kind": kind, "read_back": True, **_part(value, arguments)}

    async def search_resources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        search = FederatedSearch(self.store, ActionRegistry(self.store))
        result = await asyncio.to_thread(
            search.search,
            query=arguments.get("query", ""),
            active_project=self.project_id,
            scope=SearchScope.ACTIVE,
            kinds=[ResourceKind(item) for item in arguments.get("kinds", [])],
            cursor=arguments.get("cursor"),
            limit=20,
            exclude_private_chats=True,
        )
        return {
            "read_back": True,
            **_part(result.model_dump(mode="json"), arguments),
            "page_next_cursor": result.next_cursor,
        }

    def _operator(self) -> str:
        profile = OperatorProfileService(self.store).active_profile_or_none()
        return profile.id if profile else UNNAMED_OPERATOR

    async def guides(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "operator_key": self._operator(),
            "catalog": [
                {"id": key, "label": title, "steps": _steps}
                for key, title, _steps in GUIDES
            ],
            "progress": [
                row
                for row in await self._request("GET", "/api/v1/guides/progress")
                if row["guide_id"] in {key for key, _title, _steps in GUIDES}
            ],
            "new_progress_revision": 0,
            "read_back": True,
        }

    async def set_guide_progress(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._writable()
        if self._operator() != arguments["operator_key"]:
            raise ValueError("active operator changed; read guide progress again")
        guide = arguments["guide_id"]
        steps = next(count for key, _title, count in GUIDES if key == guide)
        if arguments["step_index"] >= steps:
            raise ValueError("choose a step from this guide catalog")
        body = {
            key: arguments[key] for key in ("status", "step_index", "expected_revision")
        }
        body["expected_operator_key"] = arguments["operator_key"]
        saved = await self._request(
            "PUT", f"/api/v1/guides/progress/{quote(guide, safe='')}", body
        )
        observed = next(
            (
                row
                for row in await self._request("GET", "/api/v1/guides/progress")
                if row["guide_id"] == guide
                and row["operator_key"] == arguments["operator_key"]
            ),
            None,
        )
        if observed != saved or self._operator() != arguments["operator_key"]:
            raise ValueError(
                "guide was changed but readback is uncertain; reread progress"
            )
        return {
            "state": "core_project_data_saved",
            "read_back": True,
            "guide": observed,
            "client_action": {"kind": "guides_refresh", "project_id": self.project_id},
        }

    async def create_starter_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._writable()
        # Deliberately exclude hook registration: it enables a local program on
        # subsequent turns and stays in the operator's guide controls.
        kind = arguments["kind"]
        if kind not in {"skill", "agents_md"}:
            raise ValueError("create hook programs in the requesting browser's guide")
        expected = starter_files(kind, arguments.get("name", ""))
        result = await self._request(
            "POST",
            "/api/v1/guides/starter-files",
            {
                "engagement_id": self.project_id,
                "kind": kind,
                "name": arguments.get("name", ""),
            },
        )
        workspace = self.core_api.app.state.workspace_service
        for path, text, _mode in expected:

            def read_template():
                download = workspace.download(self.project_id, path)
                try:
                    return download.stream.read(len(text.encode("utf-8")) + 1).decode(
                        "utf-8"
                    )
                finally:
                    download.stream.close()

            observed = await asyncio.to_thread(read_template)
            if observed != text:
                raise ValueError(
                    "starter files were created but could not be verified; inspect Files"
                )
        return {
            "read_back": True,
            "paths": result["paths"],
            "result": "Core created and verified the fixed starter templates; refresh Files to open them",
        }

    def actions(self) -> list[AnalysisTool]:
        operations: list[tuple[str, str, dict[str, Any], Any, bool]] = [
            (
                "nebula.data.system",
                "Read bounded Core health, public diagnostics/errors/files/settings/incidents, setup status and project automation runtime, recorded runtime usage, execution capabilities, terminal public IP, local issue overview, desktop browser mode, workspace Git status/diff, selected-project harness session metadata or a visible chat's side-chat labels. No sensitive detail, private transcript, arbitrary path/URL, process or account-history import. source_diff requires path; side_chats requires session_id. offset/expected_sha256 page the same stable observation.",
                _schema(
                    {
                        "kind": {
                            "enum": [
                                *_SYSTEM,
                                "source_diff",
                                "side_chats",
                                "harness_sessions",
                                "diagnostic_incidents",
                            ]
                        },
                        "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "staged": {"type": "boolean"},
                        "session_id": _ID,
                        "page_offset": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 10000,
                        },
                        **_CHUNK,
                    },
                    "kind",
                ),
                self.system,
                True,
            ),
            (
                "nebula.data.search_resources",
                "Search metadata in the selected project and shared Library using Core's canonical resource search. Private Butler, temporary and side conversations are excluded before pagination. query/kinds/cursor select a bounded page; offset/expected_sha256 read its stable chunks. Use page_next_cursor for another page. This never opens or executes a resource action.",
                _schema(
                    {
                        "query": {"type": "string", "maxLength": 500},
                        "kinds": {
                            "type": "array",
                            "maxItems": 24,
                            "items": {"enum": [kind.value for kind in ResourceKind]},
                        },
                        "cursor": {"type": "string", "maxLength": 200},
                        **_CHUNK,
                    }
                ),
                self.search_resources,
                True,
            ),
            (
                "nebula.data.guides",
                "Discover named guides and the active local operator's saved progress/revisions. An absent guide has revision zero; guide completion is progress metadata, not a security or product attestation.",
                _schema({}),
                self.guides,
                True,
            ),
            (
                "nebula.data.set_guide_progress",
                "Save one named guide's progress on explicit operator request using operator_key and expected_revision from guides. Core refuses stale progress or an operator switch. The requesting browser rereads saved progress without replacing an open guide's input.",
                _schema(
                    {
                        "guide_id": {"enum": [key for key, _title, _steps in GUIDES]},
                        "operator_key": _ID,
                        "status": {"enum": ["in_progress", "completed", "dismissed"]},
                        "step_index": {"type": "integer", "minimum": 0, "maximum": 64},
                        "expected_revision": {"type": "integer", "minimum": 0},
                    },
                    "guide_id",
                    "operator_key",
                    "status",
                    "step_index",
                    "expected_revision",
                ),
                self.set_guide_progress,
                False,
            ),
            (
                "nebula.data.create_starter_files",
                "Create the selected project's fixed skill or AGENTS.md guide templates without overwriting existing files or following symlinks; Core reads back each template. Choose a lowercase skill name. This does not enable a hook program; open the lifecycle-hooks guide in the requesting browser for that operator-owned action.",
                _schema(
                    {
                        "kind": {"enum": ["skill", "agents_md"]},
                        "name": {
                            "type": "string",
                            "maxLength": 63,
                            "pattern": "^[a-z0-9][a-z0-9-]{0,62}$",
                        },
                    },
                    "kind",
                ),
                self.create_starter_files,
                False,
            ),
        ]
        return [
            AnalysisTool(
                ToolSpec(
                    name=name,
                    description=description,
                    input_schema=schema,
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.LOCAL_READ
                    if read
                    else RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.SAFE
                    if read
                    else IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                handler,
            )
            for name, description, schema, handler, read in operations
        ]
