"""Versioned contracts shared by the host, replaceable API and project workers."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator, model_validator

from .domain import NebulaModel

IPC_PROTOCOL: Final = "nebula.runtime/v1"
MAX_EXECUTION_VERSIONS = 2
GLOBAL_EXECUTION_PROJECT = "__nebula_host_work__"
SHARED_MEMORY_BOOTSTRAP_KEY = "shared_butler_memory_v1"
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RuntimeVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$")]


def bundle_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or "\\" in value
        or "\x00" in value
        or str(path) != value
    ):
        raise ValueError("bundle assets must use normalized relative paths")
    return value


class ReleaseManifest(NebulaModel):
    format: Literal["nebula.runtime-bundle/v1"] = "nebula.runtime-bundle/v1"
    version: RuntimeVersion
    supervisor_protocol: Literal["nebula.runtime/v1"] = IPC_PROTOCOL
    api_protocol_min: int = Field(default=1, ge=1)
    api_protocol_max: int = Field(default=1, ge=1)
    execution_protocol_min: int = Field(default=1, ge=1)
    execution_protocol_max: int = Field(default=1, ge=1)
    schema_family: int = Field(ge=1)
    schema_min: int = Field(ge=1)
    schema_max: int = Field(ge=1)
    additive_migrations: bool = Field(strict=True)
    migration_head: str = Field(min_length=1, max_length=200)
    core_executable: str
    ui_directory: str
    files: dict[str, SHA256] = Field(min_length=1, max_length=100_000)
    execution_reserve_bytes: int = Field(default=256 * 1024**2, ge=16 * 1024**2)
    restorable_resources: list[str] = Field(default_factory=list, max_length=100)
    features: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9./_-]{0,99}$")]] = (
        Field(default_factory=list, max_length=32)
    )
    required_api_features: list[
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9./_-]{0,99}$")]
    ] = Field(default_factory=list, max_length=32)

    @field_validator("features", "required_api_features")
    @classmethod
    def stable_features(cls, values):
        return sorted(set(values))

    @field_validator("core_executable", "ui_directory")
    @classmethod
    def relative_assets(cls, value):
        return bundle_relative_path(value)

    @model_validator(mode="after")
    def coherent_ranges(self):
        if (
            self.api_protocol_min > self.api_protocol_max
            or self.execution_protocol_min > self.execution_protocol_max
            or self.schema_min > self.schema_max
        ):
            raise ValueError("runtime compatibility ranges must not be inverted")
        for path in self.files:
            bundle_relative_path(path)
        if self.core_executable not in self.files:
            raise ValueError(
                "the retained Core executable must be covered by the asset manifest"
            )
        if not any(path.startswith(self.ui_directory + "/") for path in self.files):
            raise ValueError(
                "the retained UI assets must be covered by the asset manifest"
            )
        return self

    def compatible_with(self, other: ReleaseManifest, *, schema_version: int) -> bool:
        return (
            self.supervisor_protocol == other.supervisor_protocol
            and self.schema_family == other.schema_family
            and self.schema_min <= schema_version <= self.schema_max
            and other.schema_min <= schema_version <= other.schema_max
            and max(self.execution_protocol_min, other.execution_protocol_min)
            <= min(self.execution_protocol_max, other.execution_protocol_max)
            and max(self.api_protocol_min, other.api_protocol_min)
            <= min(self.api_protocol_max, other.api_protocol_max)
            and self.additive_migrations
            and other.additive_migrations
        )


class HandoffPhase(StrEnum):
    REQUESTED = "requested"
    FINISHING = "finishing"
    TRANSFERABLE = "transferable"
    TRANSFERRED = "transferred"
    VERIFIED = "verified"


class ResourceSnapshot(NebulaModel):
    id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    runtime_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=100)
    state: Literal["active", "restorable", "closed", "uncertain"]
    restoration: Literal["durable", "reference", "pinned"]
    detail: str = Field(max_length=4000)
    checkpoint: dict = Field(default_factory=dict)


class WorkerIdentity(NebulaModel):
    protocol: Literal["nebula.runtime/v1"] = IPC_PROTOCOL
    runtime_id: str = Field(min_length=1, max_length=200)
    version: RuntimeVersion
    role: Literal["api", "execution"]
    pid: int = Field(ge=1)
    process_birth: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=4096)


class ProjectDeploymentStatus(NebulaModel):
    project_id: str
    runtime_id: str | None
    runtime_version: str | None
    fencing_token: int
    phase: HandoffPhase | None = None
    target_version: str | None = None
    blockers: list[str] = Field(default_factory=list)
    admission_held: bool = False


class DeploymentStatus(NebulaModel):
    protocol: Literal["nebula.runtime/v1"] = IPC_PROTOCOL
    revision: int
    api_version: str | None
    target_execution_version: str | None
    resident_versions: list[str]
    staged_versions: list[str]
    projects: list[ProjectDeploymentStatus]
    health: Literal["ready", "degraded", "starting", "staged"]
    detail: str
