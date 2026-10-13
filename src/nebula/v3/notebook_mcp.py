"""Reserved, project-scoped Notebook MCP tools routed directly through Core."""

from __future__ import annotations

import json
from typing import Any

from .domain import (
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    utc_now,
)
from .notebooks import NotebookService, NotebookError
from .storage import NebulaStore, NotFoundError


def profile_id(project_id: str) -> str:
    return f"nebula-notebook-{project_id}"


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
TOOLS = (
    (
        "status",
        "Read the project notebook runtime and consent state.",
        _schema({}),
        True,
    ),
    ("list", "List Python notebooks in this project workspace.", _schema({}), True),
    (
        "read",
        "Read a notebook, including cell IDs and revisions.",
        _schema({"path": PATH}, "path"),
        True,
    ),
    (
        "create",
        "Create a Python notebook in this project workspace.",
        _schema({"path": PATH}, "path"),
        False,
    ),
    (
        "edit_cell",
        "Add or edit a cell using its current source hash to prevent overwrites.",
        _schema(
            {
                "path": PATH,
                "cell_id": TEXT,
                "source": TEXT,
                "expected_revision": TEXT,
                "expected_source_hash": TEXT,
                "cell_type": {"enum": ["code", "markdown"]},
            },
            "path",
            "source",
        ),
        False,
    ),
    (
        "execute",
        "Queue a saved code cell for execution and return a run ID immediately.",
        _schema({"path": PATH, "cell_id": TEXT}, "path", "cell_id"),
        False,
    ),
    (
        "wait",
        "Wait briefly for a run and return ordered results; call again after a timeout.",
        _schema(
            {
                "run_id": TEXT,
                "timeout": {"type": "number", "minimum": 0, "maximum": 60},
            },
            "run_id",
        ),
        True,
    ),
    (
        "run",
        "Read a run and replay its ordered events after an optional sequence cursor.",
        _schema(
            {"run_id": TEXT, "after_sequence": {"type": "integer", "minimum": 0}},
            "run_id",
        ),
        True,
    ),
    (
        "interrupt",
        "Interrupt a queued or running cell.",
        _schema({"run_id": TEXT}, "run_id"),
        False,
    ),
    (
        "restart",
        "Restart an idle notebook kernel.",
        _schema({"path": PATH}, "path"),
        False,
    ),
    (
        "install_package",
        "Install a Python package into this project's selected Host or Docker notebook environment.",
        _schema({"path": PATH, "requirement": TEXT}, "path", "requirement"),
        False,
    ),
    (
        "search_package",
        "Look up an exact Python package name through this project's selected runtime and network policy.",
        _schema({"path": PATH, "name": TEXT}, "path", "name"),
        True,
    ),
)


class NotebookMcpTools:
    def __init__(self, store: NebulaStore, notebooks: NotebookService) -> None:
        self.store = store
        self.notebooks = notebooks

    @staticmethod
    def capabilities() -> McpCapabilitySnapshot:
        return McpCapabilitySnapshot(
            protocol_version="2025-06-18",
            checked_at=utc_now(),
            instructions="Notebook tools operate only on the selected Nebula project. Read cell revisions before editing. Host execution needs operator consent.",
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
                for name, description, schema, read_only in TOOLS
            ],
        )

    def register_profile(self, project_id: str) -> McpServerProfile:
        return self.ensure_profile(self.store, project_id)

    @classmethod
    def ensure_profile(cls, store: NebulaStore, project_id: str) -> McpServerProfile:
        from .domain import Engagement

        store.get(Engagement, project_id)
        identity = profile_id(project_id)
        try:
            current = store.get(McpServerProfile, identity)
        except NotFoundError:
            # diagnostic-expected: first use creates the reserved project profile.
            return store.create(
                McpServerProfile(
                    id=identity,
                    name="Notebook",
                    description="Edit and run Python notebook cells in this project.",
                    transport=McpTransport.BUILTIN,
                    enabled=True,
                    default_approval=McpApprovalMode.RISK_BASED,
                    capabilities=cls.capabilities(),
                    metadata={"built_in": "notebook_v1", "engagement_id": project_id},
                )
            )
        if (
            current.metadata.get("engagement_id") != project_id
            or current.transport != McpTransport.BUILTIN
        ):
            raise NotebookError("Notebook MCP profile identity is inconsistent")
        fresh = cls.capabilities()
        if current.capabilities.tools != fresh.tools:
            current = store.update(
                McpServerProfile,
                identity,
                {"capabilities": fresh.model_dump(mode="json")},
                expected_revision=current.revision,
            )
        return current

    @staticmethod
    def _result(value: Any) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": json.dumps(value, ensure_ascii=False)}
            ],
            "structuredContent": value,
            "isError": False,
        }

    async def call_tool(
        self, project_id: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        # The gateway checks the live profile grant as well; this service never
        # accepts a project ID supplied by the model in tool arguments.
        profile = self.store.get(McpServerProfile, profile_id(project_id))
        if not profile.enabled or profile.metadata.get("engagement_id") != project_id:
            raise NotebookError("Notebook MCP grant was revoked")
        path_value = arguments.get("path")
        if path_value is not None and not isinstance(path_value, str):
            raise NotebookError("notebook path must be a string")
        path = path_value or ""
        if (
            name
            in {
                "read",
                "create",
                "edit_cell",
                "execute",
                "restart",
                "install_package",
                "search_package",
            }
            and not path
        ):
            raise NotebookError("choose a notebook path")
        value: Any
        if name == "status":
            value = self.notebooks.status(project_id)
        elif name == "list":
            value = self.notebooks.list(project_id)
        elif name == "read":
            value = self.notebooks.read(project_id, path)
        elif name == "create":
            value = await self.notebooks.create(project_id, path)
        elif name == "edit_cell":
            value = await self.notebooks.upsert_cell(
                project_id,
                path,
                cell_id=arguments.get("cell_id"),
                source=arguments["source"],
                expected_revision=arguments.get("expected_revision"),
                expected_source_hash=arguments.get("expected_source_hash"),
                cell_type=arguments.get("cell_type", "code"),
            )
        elif name == "execute":
            value = (
                await self.notebooks.start_run(project_id, path, arguments["cell_id"])
            ).model_dump(mode="json")
        elif name == "wait":
            value = await self.notebooks.wait_run(
                arguments["run_id"],
                engagement_id=project_id,
                timeout=arguments.get("timeout", 30),
            )
        elif name == "run":
            value = self.notebooks.run(
                arguments["run_id"],
                engagement_id=project_id,
                after_sequence=arguments.get("after_sequence", 0),
            )
        elif name == "interrupt":
            value = (
                await self.notebooks.interrupt(
                    arguments["run_id"], engagement_id=project_id
                )
            ).model_dump(mode="json")
        elif name == "restart":
            await self.notebooks.restart(project_id, path)
            value = {"restarted": True}
        elif name == "install_package":
            value = await self.notebooks.install_package(
                project_id, path, arguments["requirement"]
            )
        elif name == "search_package":
            value = await self.notebooks.search_package(
                project_id, path, arguments["name"]
            )
        else:
            raise NotebookError("unknown Notebook tool")
        return self._result(value)
