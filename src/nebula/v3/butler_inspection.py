"""Project-owned conversation, tool-output and execution inspection for Butler."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

import httpx

from .butler_data import ButlerData, _ID, _OFFSET, _REVISION, _schema
from .domain import (
    AgentRun,
    Artifact,
    ChatMessage,
    ChatTurn,
    OperatorExecution,
    RiskClass,
    ToolCall,
    ToolCallOrigin,
)
from .tools import AnalysisTool, GuidedToolError, IdempotencyBehavior, ToolSpec
from .read_workers import read_in_worker


_CHUNK = {
    "offset": {"type": "integer", "minimum": 0, "maximum": 24000000},
    "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
}
_MAX_DOWNLOAD = 20 * 1024 * 1024


def _part(value: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(text.encode("ascii")).hexdigest()
    offset = arguments.get("offset", 0)
    expected = arguments.get("expected_sha256")
    if (offset and not expected) or (expected and expected != digest):
        raise ValueError("inspection changed; reread its first chunk before continuing")
    end = min(offset + 4096, len(text))
    return {
        "encoding": "json-ascii",
        "sha256": digest,
        "text": text[offset:end],
        "total_characters": len(text),
        "next_offset": end if end < len(text) else None,
    }


class ButlerInspection(ButlerData):
    def _turn(self, session_id: str, turn_id: str) -> ChatTurn:
        self._chat(session_id)
        turn = self.store.get(ChatTurn, turn_id)
        if turn.engagement_id != self.project_id or turn.session_id != session_id:
            raise ValueError("choose a turn from the selected chat")
        return turn

    def turns(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session = self._chat(arguments["session_id"])
        offset = arguments.get("offset", 0)
        rows = self.store.find_entities(
            ChatTurn,
            {"session_id": session.id},
            engagement_id=self.project_id,
            newest_first=True,
            offset=offset,
            limit=51,
        )
        return {
            "turns": [
                {
                    "turn_id": row.id,
                    "revision": row.revision,
                    "status": row.status.value,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    async def chat(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session = self._chat(arguments["session_id"])
        base = f"/api/v1/chat/sessions/{quote(session.id, safe='')}"
        kind = arguments["kind"]
        if kind in {"reasoning", "evidence"}:
            message = self.store.get(ChatMessage, arguments["message_id"])
            if (
                message.engagement_id != self.project_id
                or message.session_id != session.id
            ):
                raise ValueError("choose a saved message from this chat")
            path = f"{base}/messages/{quote(message.id, safe='')}/{kind}"
        elif kind in {"turn_summary", "turn_hooks"}:
            turn = self._turn(session.id, arguments["turn_id"])
            path = (
                f"{base}/turns/{quote(turn.id, safe='')}/summary"
                if kind == "turn_summary"
                else f"/api/v1/chat/turns/{quote(turn.id, safe='')}/hooks"
            )
        elif kind == "results":
            path = f"{base}/results?offset={arguments.get('page_offset', 0)}&limit=20"
        else:
            path = f"{base}/{'hooks' if kind == 'session_hooks' else 'context'}"
        value = await self._request("GET", path)
        return {
            "session_id": session.id,
            "kind": kind,
            **_part(value, arguments),
            "page_next_offset": value.get("next_offset")
            if isinstance(value, dict)
            else None,
        }

    def _call(self, call_id: str) -> ToolCall:
        call = self.store.get(ToolCall, call_id)
        if call.engagement_id != self.project_id:
            raise ValueError("choose a tool call from the selected project")
        if call.origin == ToolCallOrigin.CHAT:
            if not call.chat_session_id:
                raise ValueError("tool call has no visible conversation owner")
            self._chat(call.chat_session_id)
        else:
            run = self.store.get(AgentRun, call.run_id)
            if run.engagement_id != self.project_id:
                raise ValueError("choose a mission from the selected project")
        return call

    def calls(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if "session_id" in arguments:
            session = self._chat(arguments["session_id"])
            filters = {"chat_session_id": session.id}
        else:
            run = self.store.get(AgentRun, arguments["run_id"])
            if run.engagement_id != self.project_id:
                raise ValueError("choose a mission from the selected project")
            filters = {"run_id": run.id}
        offset = arguments.get("offset", 0)
        rows = self.store.find_entities(
            ToolCall,
            filters,
            engagement_id=self.project_id,
            newest_first=True,
            offset=offset,
            limit=51,
        )
        return {
            "calls": [
                {
                    "tool_call_id": row.id,
                    "revision": row.revision,
                    "tool_name": row.tool_name,
                    "status": row.status.value,
                    "origin": row.origin.value,
                }
                for row in rows[:50]
            ],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    @staticmethod
    def _artifact_summary(artifact: Artifact) -> dict[str, Any]:
        return {
            "artifact_id": artifact.id,
            "revision": artifact.revision,
            "filename": artifact.filename,
            "sha256": artifact.sha256,
            "size": artifact.size,
            "media_type": artifact.media_type,
            "redacted": artifact.redacted,
        }

    def artifacts(self, arguments: dict[str, Any]) -> dict[str, Any]:
        call = self._call(arguments["tool_call_id"])
        offset = arguments.get("offset", 0)
        # The SQL query restricts the page before serialization and never
        # returns storage paths, raw output, call arguments or private metadata.
        rows = self.store.find_entities(
            Artifact,
            {"metadata.tool_call_id": call.id},
            engagement_id=self.project_id,
            offset=offset,
            limit=51,
        )
        return {
            "artifacts": [self._artifact_summary(row) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def _tool_artifact(self, call_id: str, artifact_id: str) -> Artifact:
        call = self._call(call_id)
        artifact = self.store.get(Artifact, artifact_id)
        if (
            artifact.engagement_id != self.project_id
            or artifact.metadata.get("tool_call_id") != call.id
        ):
            raise ValueError("choose an artifact from the selected tool call")
        return artifact

    async def output(self, arguments: dict[str, Any]) -> dict[str, Any]:
        artifact = self._tool_artifact(
            arguments["tool_call_id"], arguments["artifact_id"]
        )
        # The existing Core service redacts complete lines before pagination.
        # Raw original downloads remain a separate requesting-browser action.
        value = await self._request(
            "POST",
            f"/api/v1/artifacts/{quote(artifact.id, safe='')}/output/read",
            {
                key: arguments[key]
                for key in ("starting_line", "line_count", "continuation_cursor")
                if key in arguments
            },
        )
        return value

    async def search_output(self, arguments: dict[str, Any]) -> dict[str, Any]:
        call = self._call(arguments["tool_call_id"])
        return await self._request(
            "POST",
            f"/api/v1/tool-calls/{quote(call.id, safe='')}/output/search",
            {
                key: arguments[key]
                for key in (
                    "query",
                    "mode",
                    "case_sensitive",
                    "context_lines",
                    "match_limit",
                    "cursor",
                )
                if key in arguments
            },
        )

    def _execution(self, execution_id: str) -> OperatorExecution:
        execution = self.store.get(OperatorExecution, execution_id)
        if execution.engagement_id != self.project_id:
            raise ValueError("choose an execution from the selected project")
        return execution

    @staticmethod
    def _execution_summary(execution: OperatorExecution) -> dict[str, Any]:
        return {
            "execution_id": execution.id,
            "revision": execution.revision,
            "created_at": execution.created_at.isoformat(),
            "language": execution.language,
            "source_preview": execution.source_preview[:500],
            "status": execution.status.value,
            "exit_code": execution.exit_code,
            "error_code": execution.error_code,
            "output_truncated": execution.output_truncated,
            "artifacts": {
                key: getattr(execution, key)
                for key in (
                    "source_artifact_id",
                    "stdout_artifact_id",
                    "stderr_artifact_id",
                    "redacted_stdout_artifact_id",
                    "redacted_stderr_artifact_id",
                    "manifest_artifact_id",
                )
            },
        }

    def executions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.find_entities(
            OperatorExecution,
            {},
            engagement_id=self.project_id,
            newest_first=True,
            offset=offset,
            limit=51,
        )
        return {
            "executions": [self._execution_summary(row) for row in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def execution(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._execution_summary(self._execution(arguments["execution_id"]))

    def execution_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        execution = self._execution(arguments["execution_id"])
        artifact = self.store.get(Artifact, arguments["artifact_id"])
        if (
            artifact.engagement_id != self.project_id
            or artifact.id
            not in self._execution_summary(execution)["artifacts"].values()
            or artifact.metadata.get("execution_id") != execution.id
        ):
            raise ValueError("choose an artifact from the selected execution")
        return self._artifact_summary(artifact)

    async def execution_output(self, arguments: dict[str, Any]) -> dict[str, Any]:
        execution = self._execution(arguments["execution_id"])
        if self.core_api is None:
            raise ValueError(
                "reconnect Butler to Core before inspecting execution output"
            )
        offset = arguments.get("offset", 0)
        stream = arguments.get("stream", "stdout")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.core_api.app),
            base_url="http://nebula-core",
            headers={"Authorization": f"Bearer {self.core_api.token}"},
            timeout=30,
        ) as client:
            response = await client.get(
                f"/api/v1/executions/{quote(execution.id, safe='')}/output/{stream}",
                params={"raw": "false", "offset": offset, "limit": 4096},
            )
        if not response.is_success:
            raise GuidedToolError(
                "Core execution output is unavailable",
                code="execution_output_unavailable",
                category="invalid_arguments",
                problem="Core did not return a verified redacted output page.",
                next_action="Read execution status and its available output artifacts before retrying. Keep raw downloads in the requesting browser.",
                before_execution=True,
            )
        return {
            "execution_id": execution.id,
            "stream": stream,
            "text": response.text,
            "redacted": True,
            "total_bytes": int(response.headers["x-nebula-output-total"]),
            "next_offset": int(response.headers["x-nebula-output-next"]),
        }

    async def cancel_execution(self, arguments: dict[str, Any]) -> dict[str, Any]:
        execution = self._execution(arguments["execution_id"])
        if execution.revision != arguments["expected_revision"]:
            raise ValueError(
                "execution changed; read its current revision before cancellation"
            )
        await self._request(
            "POST",
            f"/api/v1/executions/{quote(execution.id, safe='')}/cancel?expected_revision={execution.revision}",
        )
        observed = self._execution(execution.id)
        if observed.status.value in {"queued", "running"}:
            raise ValueError(
                "Core did not confirm cancellation; read execution status before retrying"
            )
        return {
            "state": "core_execution_saved",
            "read_back": True,
            "execution": self._execution_summary(observed),
            "client_action": {
                "kind": "execution_refresh",
                "project_id": self.project_id,
                "execution_id": execution.id,
            },
        }

    def download_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        artifact = self.store.get(Artifact, arguments["artifact_id"])
        owner_kind = arguments["owner_kind"]
        owner_id = arguments["owner_id"]
        if owner_kind == "tool_call":
            self._tool_artifact(owner_id, artifact.id)
        else:
            execution = self._execution(owner_id)
            if (
                artifact.id
                not in self._execution_summary(execution)["artifacts"].values()
                or artifact.metadata.get("execution_id") != execution.id
            ):
                raise ValueError("choose an artifact from the selected execution")
        if (
            artifact.engagement_id != self.project_id
            or artifact.revision != arguments["expected_revision"]
        ):
            raise ValueError("artifact changed; read its current identity and revision")
        if artifact.size > _MAX_DOWNLOAD:
            raise ValueError(
                "use the original artifact's browser download for files larger than 20 MiB"
            )
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "artifact_download",
                "project_id": self.project_id,
                "artifact_id": artifact.id,
                "expected_revision": artifact.revision,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "filename": (artifact.filename or "artifact")[:255],
            },
        }

    def actions(self) -> list[AnalysisTool]:
        session = {"session_id": _ID}
        tool = {"tool_call_id": _ID}
        execution = {"execution_id": _ID}
        chat_schema = _schema(
            {
                **session,
                **_CHUNK,
                "kind": {
                    "type": "string",
                    "enum": [
                        "context",
                        "reasoning",
                        "evidence",
                        "turn_summary",
                        "turn_hooks",
                        "session_hooks",
                        "results",
                    ],
                },
                "message_id": _ID,
                "turn_id": _ID,
                "page_offset": _OFFSET,
            },
            "session_id",
            "kind",
        )
        chat_schema["allOf"] = [
            {
                "if": {"properties": {"kind": {"enum": kinds}}},
                "then": {"required": [field]},
            }
            for kinds, field in [
                (["reasoning", "evidence"], "message_id"),
                (["turn_summary", "turn_hooks"], "turn_id"),
            ]
        ]
        calls_schema = _schema({**session, "run_id": _ID, "offset": _OFFSET})
        calls_schema["oneOf"] = [
            {"required": [key]} for key in ("session_id", "run_id")
        ]
        definitions = (
            (
                "nebula.inspect.turns",
                "Page saved turns of one visible project conversation before inspecting summaries and hook outcomes.",
                _schema({**session, "offset": _OFFSET}, "session_id"),
                True,
            ),
            (
                "nebula.inspect.chat",
                "Read bounded Core conversation context, message reasoning/evidence, turn summary, hook outcomes or saved result projection. Return ASCII JSON chunks; pass the returned SHA-256 on later chunks to refuse changed projections. Core result pages have separate page_next_offset.",
                chat_schema,
                True,
            ),
            (
                "nebula.inspect.calls",
                "Page tool-call identities and statuses for one visible project chat or one selected-project mission; no arguments or raw result payloads.",
                calls_schema,
                True,
            ),
            (
                "nebula.inspect.artifacts",
                "Page one selected-project tool call's original artifact identities, sizes, hashes and revisions without storage paths or bytes.",
                _schema({**tool, "offset": _OFFSET}, "tool_call_id"),
                True,
            ),
            (
                "nebula.inspect.output",
                "Read a bounded tool-output page through Core's ownership-checked redacted projection. Continue with its cursor; raw original bytes stay in the requesting browser.",
                _schema(
                    {
                        **tool,
                        "artifact_id": _ID,
                        "starting_line": {"type": "integer", "minimum": 1},
                        "line_count": {"type": "integer", "minimum": 1, "maximum": 20},
                        "continuation_cursor": {"type": "string", "maxLength": 4096},
                    },
                    "tool_call_id",
                    "artifact_id",
                ),
                True,
            ),
            (
                "nebula.inspect.search_output",
                "Search one selected-project tool call's redacted output through Core's bounded literal/regex service, preserving its continuation cursor and ownership checks.",
                _schema(
                    {
                        **tool,
                        "query": {"type": "string", "minLength": 1, "maxLength": 512},
                        "mode": {"type": "string", "enum": ["literal", "regex"]},
                        "case_sensitive": {"type": "boolean"},
                        "context_lines": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 5,
                        },
                        "match_limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        "cursor": {"type": "string", "maxLength": 4096},
                    },
                    "tool_call_id",
                    "query",
                ),
                True,
            ),
            (
                "nebula.inspect.executions",
                "Page operator execution identities, current revisions and statuses in the selected project. This never prepares or starts code execution.",
                _schema({"offset": _OFFSET}),
                True,
            ),
            (
                "nebula.inspect.execution",
                "Read one selected-project execution's status and available artifact identities without raw source or output.",
                _schema(execution, "execution_id"),
                True,
            ),
            (
                "nebula.inspect.execution_artifact",
                "Read the title, hash, size and current revision of one original or redacted artifact listed by a selected-project execution before a browser download.",
                _schema(
                    {**execution, "artifact_id": _ID}, "execution_id", "artifact_id"
                ),
                True,
            ),
            (
                "nebula.inspect.execution_output",
                "Read at most 4 KiB of Core's verified redacted stdout or stderr for one project execution, preserving UTF-8 boundaries and byte continuation offsets. Raw mode is unavailable.",
                _schema(
                    {
                        **execution,
                        "stream": {"type": "string", "enum": ["stdout", "stderr"]},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                    "execution_id",
                ),
                True,
            ),
            (
                "nebula.inspect.cancel_execution",
                "Cancel one exact selected-project execution at its freshly observed revision through Core, then read back its actual status. Cancelling is a request in progress; only a terminal status proves settlement. This cannot start or rerun code.",
                _schema(
                    {**execution, "expected_revision": _REVISION},
                    "execution_id",
                    "expected_revision",
                ),
                False,
            ),
            (
                "nebula.inspect.download_artifact",
                "Ask the requesting browser to confirm a sensitive original artifact download for a selected project tool call or execution. At most 20 MiB; verify exact ownership, revision, SHA-256 and bytes. Butler receives no original content; browser download start is not proof of OS save location.",
                _schema(
                    {
                        "owner_kind": {
                            "type": "string",
                            "enum": ["tool_call", "execution"],
                        },
                        "owner_id": _ID,
                        "artifact_id": _ID,
                        "expected_revision": _REVISION,
                    },
                    "owner_kind",
                    "owner_id",
                    "artifact_id",
                    "expected_revision",
                ),
                False,
            ),
        )
        asynchronous = {
            "chat",
            "output",
            "search_output",
            "execution_output",
            "cancel_execution",
        }
        actions = []
        for name, description, schema, read_only in definitions:

            async def run(
                arguments: dict[str, Any],
                *,
                operation: str = name.rsplit(".", 1)[-1],
                selected_read: bool = read_only,
            ) -> dict[str, Any]:
                handler = getattr(self, operation)
                if operation in asynchronous:
                    return await handler(arguments)
                if selected_read:
                    return await read_in_worker(handler, arguments)
                return handler(arguments)

            actions.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=RiskClass.LOCAL_READ
                        if read_only
                        else RiskClass.WORKSPACE_WRITE,
                        idempotency=IdempotencyBehavior.SAFE
                        if read_only
                        else IdempotencyBehavior.NON_IDEMPOTENT,
                    ),
                    run,
                )
            )
        return actions
