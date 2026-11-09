"""Bounded, authenticated deployment operations on the stable listener."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import Field, ValidationError
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from .deployment_protocol import RuntimeVersion
from .domain import NebulaModel
from .runtime_coordination import ProjectLease

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")]
Revision = Annotated[int, Field(ge=0, strict=True)]


class StageParameters(NebulaModel):
    source: str = Field(min_length=1, max_length=4096)
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class VersionParameters(NebulaModel):
    version: RuntimeVersion


class VersionRevisionParameters(VersionParameters):
    expected_revision: Revision


class RuntimeParameters(NebulaModel):
    runtime_id: Identifier


class PromoteParameters(RuntimeParameters):
    expected_revision: Revision


class HandoffParameters(NebulaModel):
    project_id: Identifier
    target_runtime_id: Identifier


class OwnerParameters(NebulaModel):
    project_id: Identifier
    runtime_id: Identifier
    fencing_token: Annotated[int, Field(ge=1, strict=True)]


class RecoveryParameters(HandoffParameters):
    expected_owner: OwnerParameters
    expected_handoff_revision: Revision | None = None


class ShutdownParameters(NebulaModel):
    expected_revision: Revision


_PARAMETERS: dict[str, type[NebulaModel]] = {
    "stage": StageParameters,
    "select_recovery_release": VersionParameters,
    "migrate": VersionRevisionParameters,
    "prepare_api": VersionParameters,
    "promote_api": PromoteParameters,
    "target_execution": VersionParameters,
    "prepare_execution": VersionParameters,
    "handoff": HandoffParameters,
    "recover_project": RecoveryParameters,
    "rollback_code": VersionRevisionParameters,
    "retire": RuntimeParameters,
    "shutdown": ShutdownParameters,
}


class DeploymentOperationRequest(NebulaModel):
    operation_id: Identifier
    action: Literal[
        "stage",
        "select_recovery_release",
        "migrate",
        "prepare_api",
        "promote_api",
        "target_execution",
        "prepare_execution",
        "handoff",
        "recover_project",
        "rollback_code",
        "retire",
        "shutdown",
    ]
    parameters: dict


class RuntimeManagement:
    def __init__(self, supervisor, app, authentication):
        self.supervisor = supervisor
        self.operations = None
        self.slots = asyncio.Semaphore(8)
        self._shutdown_ready: set[str] = set()

        async def principal(request):
            await authentication.require_auth(
                request, await authentication.bearer(request)
            )
            device = getattr(request.state, "auth_device_id", None)
            return "device:" + device if device else "operator"

        @app.post("/api/v1/deployment/operations")
        async def operation(request: Request):
            actor = await principal(request)
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 128 * 1024:
                    raise HTTPException(413, "Deployment operation exceeds 128 KiB")
                body.extend(chunk)
            try:
                command = DeploymentOperationRequest.model_validate_json(bytes(body))
                parameters = _PARAMETERS[command.action].model_validate(
                    command.parameters
                )
            except ValidationError:
                raise HTTPException(
                    422, "Invalid deployment operation parameters"
                ) from None
            if self.slots.locked():
                raise HTTPException(
                    429, "Deployment operations are busy; read the saved operation"
                )
            async with self.slots:

                async def dispatch(action, values):
                    result = await self.dispatch(
                        action, values, command.operation_id, actor
                    )
                    if action == "shutdown":
                        self._shutdown_ready.add(command.operation_id)
                    return result

                result = await self.ledger().execute(
                    operation_id=command.operation_id,
                    actor_id=actor,
                    action=command.action,
                    parameters=parameters.model_dump(mode="json"),
                    dispatch=dispatch,
                )
            response = JSONResponse(
                result, headers={"Cache-Control": "private, no-store"}
            )
            if (
                result.get("state") == "succeeded"
                and command.operation_id in self._shutdown_ready
            ):
                response.background = BackgroundTask(
                    self.notify_shutdown, command.operation_id
                )
            return response

        @app.get("/api/v1/deployment/operations/{operation_id}")
        async def status(operation_id: str, request: Request):
            actor = await principal(request)
            result = await self.ledger().status(operation_id, actor_id=actor)
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    def ledger(self):
        if self.operations is None:
            from .runtime_operations import DeploymentOperations

            self.operations = DeploymentOperations(self.supervisor.coordinator)
        return self.operations

    async def notify_shutdown(self, operation_id):
        if operation_id in self._shutdown_ready:
            self._shutdown_ready.remove(operation_id)
            await self.supervisor._shutdown_requested()

    async def dispatch(self, action, values, operation_id, actor):
        root = self.supervisor
        if action == "stage":
            manifest = await root.stage(
                Path(values["source"]), manifest_sha256=values["manifest_sha256"]
            )
            return {
                "version": manifest.version,
                "manifest_sha256": values["manifest_sha256"],
            }
        if action == "select_recovery_release":
            await root.select_recovery_release(values["version"])
            return {"recovery_version": values["version"]}
        if action == "migrate":
            return await root.host.migrate(**values)
        if action in {"prepare_api", "prepare_execution"}:
            worker = await getattr(root, action)(**values)
            return worker.model_dump(mode="json")
        if action == "promote_api":
            await root.promote_api(**values)
        elif action == "target_execution":
            await root.coordinator.target_execution(**values)
        elif action == "handoff":
            await root.request_project_handoff(**values)
        elif action == "recover_project":
            values = {
                **values,
                "expected_owner": ProjectLease(**values["expected_owner"]),
            }
            lease = await root.recover_project(
                **values, operation_id=operation_id, actor_id=actor
            )
            return {
                "project_id": lease.project_id,
                "runtime_id": lease.runtime_id,
                "fencing_token": lease.fencing_token,
            }
        elif action == "rollback_code":
            await root.rollback_code(**values)
        elif action == "retire":
            identity = await root.host.retire(**values)
            await root.proxy.discard_endpoint(identity.endpoint)
            return identity.model_dump(mode="json")
        elif action == "shutdown":
            if root._shutdown_requested is None:
                raise HTTPException(
                    409, "This host has no coordinated shutdown callback"
                )
            return await root.shutdown(**values)
        return (await root.coordinator.status()).model_dump(mode="json")
