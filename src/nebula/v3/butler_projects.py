"""Typed interactive Butler handoffs for the operator's project switcher."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .domain import Engagement, EngagementStatus, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_REVISION = {"type": "integer", "minimum": 1}
_NAME = {"type": "string", "minLength": 1, "maxLength": 300}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_TARGET = _schema({"project_id": _ID}, "project_id")
_CREATE = _schema(
    {
        "name": _NAME,
        "description": {"type": "string", "maxLength": 20000},
        "client_name": {"type": "string", "maxLength": 300},
    },
    "name",
)
_RENAME = _schema(
    {"project_id": _ID, "expected_revision": _REVISION, "name": _NAME},
    "project_id",
    "expected_revision",
    "name",
)
_CHANGE = _schema(
    {"project_id": _ID, "expected_revision": _REVISION},
    "project_id",
    "expected_revision",
)


class ButlerProjects:
    """Prepare browser actions against exact Core projects and revisions.

    These tools exist only for an interactive operator turn. The requesting
    browser performs writes and owns confirmations and selection changes.
    """

    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    @staticmethod
    def _summary(project: Engagement) -> dict[str, Any]:
        return {
            "project_id": project.id,
            "name": project.name,
            "status": project.status.value,
            "revision": project.revision,
            "client_name": project.client_name,
            "url": f"/projects/{project.id}/workbench?view=chat",
        }

    def list_projects(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset must be from 0 to 10000")
        rows = self.store.list_entities(Engagement, offset=offset, limit=51)
        return {
            "projects": [self._summary(item) for item in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def detail(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self.store.get(Engagement, arguments["project_id"])
        return {
            **self._summary(project),
            "description": project.description[:20000],
            "workspace_linked": bool(project.workspace_path),
        }

    @staticmethod
    def _pending(action: dict[str, Any]) -> dict[str, Any]:
        return {"state": "pending_browser_apply", "client_action": action}

    def create(
        self, arguments: dict[str, Any], *, linked_folder: bool = False
    ) -> dict[str, Any]:
        name = arguments["name"].strip()
        if not name:
            raise ValueError("project name cannot be blank")
        return self._pending(
            {
                "kind": "project_create_linked" if linked_folder else "project_create",
                "name": name,
                "description": arguments.get("description", ""),
                "client_name": arguments.get("client_name", ""),
                "request_id": str(uuid4()),
            }
        )

    def _current(self, arguments: dict[str, Any]) -> Engagement:
        project = self.store.get(Engagement, arguments["project_id"])
        if project.revision != arguments["expected_revision"]:
            raise ValueError("project changed; read its current revision before acting")
        return project

    def rename(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self._current(arguments)
        name = arguments["name"].strip()
        if not name:
            raise ValueError("project name cannot be blank")
        return self._pending(
            {
                "kind": "project_rename",
                "project_id": project.id,
                "expected_revision": project.revision,
                "old_name": project.name,
                "name": name,
            }
        )

    def archive(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self._current(arguments)
        if project.status == EngagementStatus.ARCHIVED:
            raise ValueError("project is already archived; read it again")
        return self._pending(
            {
                "kind": "project_archive",
                "project_id": project.id,
                "expected_revision": project.revision,
                "name": project.name,
            }
        )

    def restore(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self._current(arguments)
        if project.status != EngagementStatus.ARCHIVED:
            raise ValueError("choose an archived project to restore")
        return self._pending(
            {
                "kind": "project_restore",
                "project_id": project.id,
                "expected_revision": project.revision,
                "name": project.name,
            }
        )

    def delete(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project = self._current(arguments)
        if project.status != EngagementStatus.ARCHIVED:
            raise ValueError("archive the project before permanently deleting it")
        return self._pending(
            {
                "kind": "project_delete",
                "project_id": project.id,
                "expected_revision": project.revision,
                "name": project.name,
            }
        )

    def plugins(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.projects.list",
                "Page all operator-visible projects, including archived projects, before choosing an exact target.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.list_projects,
            ),
            (
                "nebula.projects.detail",
                "Read one project's current name, status and revision before a lifecycle change.",
                _TARGET,
                RiskClass.LOCAL_READ,
                self.detail,
            ),
            (
                "nebula.projects.create",
                "Ask the requesting browser to create a project after explicit operator instruction. The action carries one durable retry ID. No host folder is linked and Host access remains unacknowledged; the browser reports the saved Core result. Inspect the project list after an uncertain response before retrying.",
                _CREATE,
                RiskClass.WORKSPACE_WRITE,
                self.create,
            ),
            (
                "nebula.projects.create_linked",
                "Ask the requesting browser to browse and select a Nebula-host folder for a new project. The model supplies a project name, not an absolute path. Core resolves the folder and saves it with one durable create retry ID. Host command access remains unacknowledged. Inspect the project list after an uncertain response before retrying.",
                _CREATE,
                RiskClass.WORKSPACE_WRITE,
                lambda arguments: self.create(arguments, linked_folder=True),
            ),
            (
                "nebula.projects.rename",
                "Ask the requesting browser to rename an exact project using its current revision, then read back Core and refresh the switcher.",
                _RENAME,
                RiskClass.WORKSPACE_WRITE,
                self.rename,
            ),
            (
                "nebula.projects.archive",
                "Ask the requesting browser to show the project-specific archive confirmation, then save only at the current revision and read back Core. Running work continues.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.archive,
            ),
            (
                "nebula.projects.restore",
                "Ask the requesting browser to restore an archived project at its current revision, then read back Core and refresh the switcher.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.restore,
            ),
            (
                "nebula.projects.delete",
                "Ask the requesting browser for the irreversible deletion confirmation on an archived project. Core still checks the revision and dependent records; workspace files are retained.",
                _CHANGE,
                RiskClass.WORKSPACE_WRITE,
                self.delete,
            ),
        )
        plugins: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any], *, selected_handler: Any = handler
            ) -> dict[str, Any]:
                return selected_handler(arguments)

            plugins.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=risk,
                        idempotency=IdempotencyBehavior.SAFE
                        if risk == RiskClass.LOCAL_READ
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return plugins
