"""Bounded, project-scoped Notebook actions for interactive Butler turns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .domain import RiskClass
from .notebook_mcp import NotebookMcpTools, TOOLS as NOTEBOOK_TOOLS
from .notebooks import _cell_source
from .tools import IdempotencyBehavior, ToolSpec


_PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
_CELL = {"type": "string", "minLength": 1, "maxLength": 200}
_OFFSET = {"type": "integer", "minimum": 0, "maximum": 1_000_000}
_REVISION = {"type": "string", "minLength": 1, "maxLength": 128}
_SOURCE_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_KERNEL_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}


def _schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_SCHEMAS: dict[str, dict[str, Any]] = {
    "status": _schema({}),
    "list": _schema({"offset": {"type": "integer", "minimum": 0, "maximum": 1000}}),
    "read": _schema({"path": _PATH, "offset": _OFFSET}, "path"),
    "source": _schema(
        {"path": _PATH, "cell_id": _CELL, "offset": _OFFSET},
        "path",
        "cell_id",
    ),
    "create": _schema({"path": _PATH}, "path"),
    "edit_cell": _schema(
        {
            "path": _PATH,
            "cell_id": _CELL,
            "source": {"type": "string", "maxLength": 50_000},
            "cell_type": {"type": "string", "enum": ["code", "markdown"]},
            "expected_revision": _REVISION,
            "expected_source_hash": _REVISION,
        },
        "path",
        "source",
        "cell_type",
    ),
    "execute": _schema(
        {"path": _PATH, "cell_id": _CELL, "expected_source_hash": _SOURCE_HASH},
        "path",
        "cell_id",
        "expected_source_hash",
    ),
    "recent_runs": _schema({"path": _PATH}, "path"),
    "run": _schema(
        {"run_id": _CELL, "after_sequence": {"type": "integer", "minimum": 0}},
        "run_id",
    ),
    "run_event": _schema(
        {
            "run_id": _CELL,
            "sequence": {"type": "integer", "minimum": 1},
            "offset": _OFFSET,
        },
        "run_id",
        "sequence",
    ),
    "wait": _schema(
        {"run_id": _CELL, "timeout": {"type": "number", "minimum": 0, "maximum": 25}},
        "run_id",
    ),
    "interrupt": _schema({"run_id": _CELL}, "run_id"),
    "kernel": _schema({"path": _PATH}, "path"),
    "restart": _schema(
        {
            "path": _PATH,
            "expected_notebook_revision": _SOURCE_HASH,
            "expected_kernel_id": _KERNEL_ID,
            "expected_restart_count": {"type": "integer", "minimum": 0},
        },
        "path",
        "expected_notebook_revision",
        "expected_kernel_id",
        "expected_restart_count",
    ),
    "package_status": _schema({"path": _PATH, "name": _CELL}, "path", "name"),
    "install_package": _schema(
        {
            "path": _PATH,
            "requirement": _CELL,
            "expected_notebook_revision": _SOURCE_HASH,
        },
        "path",
        "requirement",
        "expected_notebook_revision",
    ),
    "search_package": _schema({"path": _PATH, "name": _CELL}, "path", "name"),
}

_DESCRIPTIONS = {
    "status": "Read the selected project's Notebook runtime, grant, and consent state.",
    "list": "Page project Notebook paths before choosing an exact file; no host path is accepted.",
    "read": "Page bounded cell previews and hashes from one project Notebook. Use source for exact cell text.",
    "source": "Read a bounded exact source chunk from one cell after selecting its ID and current hash.",
    "create": "Create one project-relative Notebook; a duplicate path fails without replacing it. Read the saved file afterward.",
    "edit_cell": "Add with expected document revision or edit with the current cell source hash. A stale write fails without overwriting the cell.",
    "execute": "After the operator asks to run a saved code cell, queue the exact current source hash in this project. Read the run ID and wait for Core's result; if the response is uncertain, inspect recent_runs before another execution.",
    "recent_runs": "Read up to 20 recent run IDs, statuses, and source hashes for one project Notebook before retrying an uncertain execution.",
    "run": "Page ordered Core Notebook run events and current status after an execution or interruption.",
    "run_event": "Read an exact bounded text chunk of one saved Notebook run event.",
    "wait": "Wait briefly for one Core Notebook run, then read its current status and bounded events.",
    "interrupt": "After the operator asks to stop one exact queued or running Notebook run, interrupt it by run ID and read its current Core status. A running worker may acknowledge interruption later.",
    "kernel": "Read one selected Notebook's current file revision, live kernel identity, restart count and active run. An absent kernel has nothing to restart.",
    "restart": "Restart one idle kernel after reading its current Notebook revision and kernel identity. Core rejects a changed file or kernel; read the incremented restart count before reporting success.",
    "package_status": "Read one installed distribution name and version from the selected Project Notebook environment before retrying an uncertain installation.",
    "install_package": "Install an exact package requirement in the selected Project's Notebook environment after reading its file revision. Project Host consent and network policy apply; Core reads the installed distribution back. Check package_status before retrying an uncertain result.",
    "search_package": "Look up an exact package name through the project's Notebook network policy.",
}

_READS = frozenset(
    {
        "status",
        "list",
        "read",
        "source",
        "recent_runs",
        "run",
        "run_event",
        "wait",
        "kernel",
        "package_status",
        "search_package",
    }
)
_NETWORK_ACTIONS = frozenset({"execute", "install_package", "search_package"})


@dataclass(frozen=True)
class NotebookAction:
    """Catalog entry delegated to Core, rather than a broker AnalysisTool."""

    spec: ToolSpec
    _handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ButlerNotebook:
    def __init__(self, store: Any, notebooks: Any, project_id: str) -> None:
        self.project_id = project_id
        self.mcp = NotebookMcpTools(store, notebooks)

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.mcp.call_tool(self.project_id, name, arguments)
        if result.get("isError"):
            raise ValueError(
                f"Notebook {name} failed; read its current state before retrying"
            )
        return result["structuredContent"]

    @staticmethod
    def _document_summary(value: dict[str, Any], offset: int = 0) -> dict[str, Any]:
        cells = value["document"]["cells"]
        page = cells[offset : offset + 20]
        result = []
        for cell in page:
            source = _cell_source(cell)
            result.append(
                {
                    "id": cell["id"],
                    "cell_type": cell.get("cell_type"),
                    "source_preview": source[:400],
                    "source_length": len(source),
                    "source_hash": value["cell_revisions"][cell["id"]],
                    "output_count": len(cell.get("outputs") or []),
                }
            )
        return {
            "path": value["path"],
            "revision": value["revision"],
            "cells": result,
            "total_cells": len(cells),
            "next_offset": offset + len(page)
            if offset + len(page) < len(cells)
            else None,
        }

    @staticmethod
    def _run_page(value: dict[str, Any]) -> dict[str, Any]:
        events = value.get("events") or []
        page = events[:20]
        return {
            "run": value.get("run"),
            "events": [
                {
                    "sequence": item.get("sequence"),
                    "event_type": item.get("event_type"),
                    "payload_preview": json.dumps(
                        item.get("payload"), ensure_ascii=False
                    )[:1200],
                }
                for item in page
            ],
            "next_sequence": page[-1]["sequence"]
            if page
            else value.get("next_sequence"),
            "has_more": len(events) > len(page),
        }

    async def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list":
            rows = await self._call("list", {})
            offset = arguments.get("offset", 0)
            page = rows[offset : offset + 50]
            return {
                "notebooks": page,
                "total": len(rows),
                "next_offset": offset + len(page)
                if offset + len(page) < len(rows)
                else None,
            }
        if name in {"read", "source"}:
            value = await self._call("read", {"path": arguments["path"]})
            if name == "read":
                return self._document_summary(value, arguments.get("offset", 0))
            cell = next(
                (
                    item
                    for item in value["document"]["cells"]
                    if item["id"] == arguments["cell_id"]
                ),
                None,
            )
            if cell is None:
                raise ValueError("cell changed or was deleted; read the Notebook again")
            source = _cell_source(cell)
            offset = arguments.get("offset", 0)
            chunk = source[offset : offset + 12_000]
            return {
                "path": value["path"],
                "revision": value["revision"],
                "cell_id": cell["id"],
                "source_hash": value["cell_revisions"][cell["id"]],
                "source": chunk,
                "source_length": len(source),
                "next_offset": offset + len(chunk)
                if offset + len(chunk) < len(source)
                else None,
            }
        if name == "recent_runs":
            runs = await self._call("recent_runs", arguments)
            return {
                "path": arguments["path"],
                "runs": [
                    {
                        "id": run["id"],
                        "cell_id": run["cell_id"],
                        "source_hash": run["source_hash"],
                        "status": run["status"],
                        "revision": run["revision"],
                        "created_at": run.get("created_at"),
                        "completed_at": run.get("completed_at"),
                    }
                    for run in runs[:20]
                ],
            }
        if name in {"execute", "interrupt"}:
            changed = await self._call(name, arguments)
            value = await self._call("run", {"run_id": changed["id"]})
            return self._run_page(value)
        if name == "restart":
            before = await self._call("kernel", {"path": arguments["path"]})
            if before["state"] == "absent":
                raise ValueError(
                    "no live kernel exists; run a cell before asking to restart it"
                )
            if (
                before["notebook_revision"] != arguments["expected_notebook_revision"]
                or before["kernel_id"] != arguments["expected_kernel_id"]
                or before["restart_count"] != arguments["expected_restart_count"]
            ):
                raise ValueError(
                    "Notebook or kernel changed; read it again before restarting"
                )
            await self._call("restart", arguments)
            observed = await self._call("kernel", {"path": arguments["path"]})
            if (
                observed["kernel_id"] != before["kernel_id"]
                or observed["restart_count"] != before["restart_count"] + 1
            ):
                raise ValueError(
                    "kernel restart result changed before read-back; inspect current kernel state"
                )
            return {"restarted": True, "kernel": observed}
        if name == "install_package":
            current = await self._call("read", {"path": arguments["path"]})
            if current["revision"] != arguments["expected_notebook_revision"]:
                raise ValueError(
                    "Notebook changed; read it before installing a package"
                )
            changed = await self._call("install_package", arguments)
            name = re.split(r"\[|==", arguments["requirement"], maxsplit=1)[0]
            observed = await self._call(
                "package_status", {"path": arguments["path"], "name": name}
            )
            if not observed["installed"] or observed["version"] != changed["version"]:
                raise ValueError(
                    "package install result changed before read-back; inspect package_status before retrying"
                )
            return {
                "requirement": changed["requirement"],
                "installed": True,
                "package": observed,
                "restart_recommended": changed["restart_recommended"],
            }
        if name in {"run", "run_event", "wait"}:
            requested = "run" if name == "run_event" else name
            params = {"run_id": arguments["run_id"]}
            if requested == "run":
                params["after_sequence"] = (
                    arguments["sequence"] - 1
                    if name == "run_event"
                    else arguments.get("after_sequence", 0)
                )
            else:
                params["timeout"] = arguments.get("timeout", 0)
            value = await self._call(requested, params)
            if name != "run_event":
                return self._run_page(value)
            event = next(
                (
                    row
                    for row in value.get("events", [])
                    if row.get("sequence") == arguments["sequence"]
                ),
                None,
            )
            if event is None:
                raise ValueError(
                    "run event is unavailable; read the current run cursor"
                )
            payload = json.dumps(event.get("payload"), ensure_ascii=False)
            offset = arguments.get("offset", 0)
            chunk = payload[offset : offset + 12_000]
            return {
                "run_id": arguments["run_id"],
                "sequence": arguments["sequence"],
                "payload": chunk,
                "payload_length": len(payload),
                "next_offset": offset + len(chunk)
                if offset + len(chunk) < len(payload)
                else None,
            }
        value = await self._call(name, arguments)
        if name in {"create", "edit_cell"}:
            result = self._document_summary(value)
            if name == "edit_cell":
                result["cell_id"] = value["cell_id"]
            return result
        if isinstance(value, dict):
            return value
        return {"result": value}

    def actions(self) -> list[NotebookAction]:
        actions = []
        for name, schema in _SCHEMAS.items():

            async def run(
                arguments: dict[str, Any], *, selected: str = name
            ) -> dict[str, Any]:
                return await self.invoke(selected, arguments)

            read_only = name in _READS
            actions.append(
                NotebookAction(
                    ToolSpec(
                        name=f"nebula.notebook.{name}",
                        description=_DESCRIPTIONS[name],
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=(
                            RiskClass.PASSIVE
                            if name == "search_package"
                            else RiskClass.LOCAL_READ
                            if read_only
                            else RiskClass.WORKSPACE_WRITE
                        ),
                        filesystem_access="read" if read_only else "workspace_write",
                        network_access=name in _NETWORK_ACTIONS,
                        opaque_network_access=name in _NETWORK_ACTIONS,
                        idempotency=(
                            IdempotencyBehavior.SAFE
                            if read_only
                            else IdempotencyBehavior.NON_IDEMPOTENT
                        ),
                    ),
                    run,
                )
            )
        assert {name for name in _SCHEMAS} - {name for name, *_ in NOTEBOOK_TOOLS} == {
            "source",
            "run_event",
        }
        return actions
