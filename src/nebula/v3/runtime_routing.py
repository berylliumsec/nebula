"""Resolve public references to SQL project ownership, never to a default owner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote

from sqlalchemy import select

from .database import (
    DeploymentHostRow,
    EntityRow,
    ProjectOwnershipRow,
    RuntimeInstanceRow,
    RuntimeProcessRow,
    RuntimeResourceRow,
)
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .runtime_coordination import CoordinationConflict, RuntimeCoordination
from .runtime_delivery import HttpIntent
from .runtime_http import HttpUpstream
from .runtime_ipc import RuntimeCredentials, RuntimeIpcError


def _references(path: str, query: bytes, payload) -> tuple[set[str], set[str]]:
    segments = [unquote(part) for part in path.split("/") if part]
    if len(segments) > 64:
        raise CoordinationConflict("Request path contains too many references")
    references = {part for part in segments if len(part) <= 200}
    explicit = {
        segments[index + 1]
        for index, part in enumerate(segments[:-1])
        if part == "engagements"
    }

    def add(name, value):
        if isinstance(value, str) and value and len(value) <= 200:
            if name in {"engagement_id", "project_id"}:
                explicit.add(value)
            if name.endswith(("_id", "_ids")) or name in {"id", "ids"}:
                references.add(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    add(name, item)

    try:
        for key, value in parse_qsl(query.decode("ascii"), max_num_fields=128):
            add(key, value)
    except (ValueError, UnicodeError):
        raise CoordinationConflict(
            "Request query contains too many or invalid references"
        ) from None
    pending, visited = [(payload, 0)], 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > 4096 or depth > 24 or len(references) > 256:
            raise CoordinationConflict("Request contains too many nested references")
        if isinstance(value, dict):
            for key, child in value.items():
                add(key, child)
                if isinstance(child, (dict, list)):
                    pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend(
                (child, depth + 1) for child in value if isinstance(child, (dict, list))
            )
    return references | explicit, explicit


class RuntimeRouting:
    def __init__(self, coordination: RuntimeCoordination, credential_directory: Path):
        self.coordination = coordination
        self.credentials = RuntimeCredentials(credential_directory)

    async def _project(self, path: str, query: bytes, payload=None) -> str:
        references, explicit = _references(path, query, payload)
        async with self.coordination.database.session() as session:
            rows = list(
                await session.scalars(
                    select(EntityRow).where(EntityRow.id.in_(references))
                )
            )
            projects: set[str] = set()
            for row in rows:
                if row.kind == "engagements":
                    projects.add(row.id)
                elif row.engagement_id is not None:
                    projects.add(row.engagement_id)
            known_projects = {row.id for row in rows if row.kind == "engagements"}
            if explicit - known_projects:
                raise CoordinationConflict("The referenced project does not exist")
            projects.update(
                await session.scalars(
                    select(RuntimeResourceRow.project_id).where(
                        RuntimeResourceRow.id.in_(references),
                        RuntimeResourceRow.state != "closed",
                    )
                )
            )
        if len(projects) > 1:
            raise CoordinationConflict(
                "This request references multiple projects; choose one project"
            )
        return next(iter(projects), GLOBAL_EXECUTION_PROJECT)

    async def project_for(self, intent: HttpIntent) -> str:
        payload = None
        content_type = next(
            (value for name, value in intent.headers if name == "content-type"), ""
        )
        if (
            content_type.split(";", 1)[0].strip() == "application/json"
            and intent.body_base64
        ):
            try:
                payload = json.loads(intent.body())
            except (ValueError, UnicodeError, RecursionError):
                raise CoordinationConflict(
                    "Invalid JSON cannot be routed to an execution owner"
                ) from None
        return await self._project(intent.path, intent.query.encode("ascii"), payload)

    async def _target(
        self,
        session,
        runtime_id: str | None,
        role: str,
        lane: Literal["read", "work", "control"],
    ) -> HttpUpstream:
        process = (
            await session.get(RuntimeProcessRow, runtime_id) if runtime_id else None
        )
        instance = (
            await session.get(RuntimeInstanceRow, runtime_id) if runtime_id else None
        )
        now = await self.coordination._clock_ms(session)
        if (
            process is None
            or instance is None
            or process.role != role
            or instance.role != role
            or process.state != "ready"
            or instance.lease_expires_at_ms <= now
        ):
            raise RuntimeIpcError(
                "The authoritative runtime is unavailable; saved work remains queued"
            )
        return HttpUpstream(
            process.endpoint,
            self.credentials.read(process.credential_reference),
            lane=lane,
        )

    async def upstream_for(self, scope: dict) -> HttpUpstream:
        project = await self._project(scope["path"], scope.get("query_string", b""))
        async with self.coordination.database.session() as session:
            owner = await session.get(ProjectOwnershipRow, project)
            now = await self.coordination._clock_ms(session)
            if owner is None or owner.lease_expires_at_ms <= now:
                raise RuntimeIpcError("The project has no live execution owner")
            lane: Literal["read", "control"] = (
                "read"
                if scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS"}
                else "control"
            )
            return await self._target(session, owner.runtime_id, "execution", lane)

    async def api_upstream(self, _scope: dict) -> HttpUpstream:
        async with self.coordination.database.session() as session:
            host = await session.get(DeploymentHostRow, "host")
            return await self._target(
                session, host.api_runtime_id if host else None, "api", "read"
            )
