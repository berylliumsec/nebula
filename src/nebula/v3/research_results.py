"""Closed, bounded adapter to the Apple R&D durable-result retrieval API."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


HANDLE = re.compile(r"^(?:sim:[0-9TZ-]+[0-9a-f]{8}|mac:macos-[A-Za-z0-9._-]{1,120})$")
MATCH_ID = re.compile(r"^[1-9][0-9]*:[0-9a-f]{16}$")
FILTERS = frozenset({"tool", "artifact_role", "record_kind", "function", "address",
                     "event_kind", "fact_kind", "status", "authority"})
MAX_RESPONSE_BYTES = 64 * 1024


class ResearchResultService:
    """Invoke only catalog, search, or read with opaque handles and typed args."""

    def __init__(self, python: Path | None = None) -> None:
        self.python = python or Path(os.environ.get(
            "NEBULA_RESEARCH_RESULT_PYTHON", "/home/agent/apple_security_rnd/.venv/bin/python"
        ))

    def query(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if action not in {"catalog", "search", "read"}:
            raise ValueError("unknown research result operation")
        handle = arguments.get("handle")
        if not isinstance(handle, str) or HANDLE.fullmatch(handle) is None:
            raise ValueError("invalid research result handle")
        command = [str(self.python), "-m", "apple_rnd.workflows.result_catalog",
                   action, "--handle", handle]
        if action == "search":
            text = arguments.get("text")
            filters = arguments.get("filters") or {}
            if text is not None:
                if not isinstance(text, str) or not 1 <= len(text) <= 4096:
                    raise ValueError("invalid literal query")
                command.extend(("--text", text))
            if not isinstance(filters, dict) or set(filters) - FILTERS or any(
                not isinstance(value, str) or not value for value in filters.values()
            ):
                raise ValueError("invalid research result filters")
            command.extend(("--filters", json.dumps(filters, separators=(",", ":"))))
            limit = arguments.get("limit", 20)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                raise ValueError("invalid research result limit")
            command.extend(("--limit", str(limit)))
            cursor = arguments.get("cursor")
            if cursor is not None:
                if not isinstance(cursor, str) or len(cursor) > 8192:
                    raise ValueError("invalid research result cursor")
                command.extend(("--cursor", cursor))
        elif action == "read":
            match_id = arguments.get("match_id")
            if not isinstance(match_id, str) or MATCH_ID.fullmatch(match_id) is None:
                raise ValueError("invalid research result match ID")
            context = arguments.get("context_lines", 0)
            if not isinstance(context, int) or isinstance(context, bool) or not 0 <= context <= 5:
                raise ValueError("invalid research result context")
            command.extend(("--match-id", match_id, "--context-lines", str(context)))
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False, timeout=900)
        if result.returncode:
            raise RuntimeError(f"research result {action} failed: {result.stderr[-2048:]}")
        if len(result.stdout.encode()) > MAX_RESPONSE_BYTES:
            raise RuntimeError("research result response exceeded 64 KiB")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("handle") != handle:
            raise RuntimeError("research result adapter returned a mismatched handle")
        return payload
