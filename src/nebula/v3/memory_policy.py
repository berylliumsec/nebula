"""Validated memory policy and the runner's observable enforcement contract."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .domain import NebulaModel

MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_BYTES = 1024**5
MemoryBytes = Annotated[int, Field(strict=True, ge=16 * MIB, le=MAX_BYTES)]
SwapBytes = Annotated[int, Field(strict=True, ge=0, le=MAX_BYTES)]
ExecutionEnvironment = Literal["local", "container", "ssh", "remote", "cpu-worker"]
MemoryTerminationReason = Literal[
    "completed",
    "cancelled",
    "launch_failed",
    "worker_lost",
    "memory_limit_exceeded",
    "failed",
    "running",
]


class MemoryBudgetError(ValueError):
    pass


class MemoryBudgetConflict(MemoryBudgetError):
    pass


class MemoryEnforcementUnavailable(MemoryBudgetError):
    pass


class MemoryLaunchOwner(NebulaModel):
    process_id: int = Field(strict=True, ge=1)
    process_birth: str = Field(min_length=1, max_length=200)
    runner_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    process_origin: str | None = Field(default=None, max_length=200)
    runner_identity: str | None = Field(default=None, max_length=300)


class MemoryLaunchGroups(NebulaModel):
    group_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    auxiliary_group_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(
        default_factory=list, max_length=8
    )


class MemoryLimits(NebulaModel):
    max_bytes: MemoryBytes
    high_bytes: MemoryBytes | None = None
    swap_bytes: SwapBytes = 0

    @property
    def reservation_bytes(self) -> int:
        # Conservatively charge swap too: concurrent jobs must not turn an
        # explicit disk-backed allowance into unaccounted host pressure.
        return self.max_bytes + self.swap_bytes

    @model_validator(mode="after")
    def pressure_is_below_ceiling(self):
        if self.high_bytes is not None and self.high_bytes > self.max_bytes:
            raise ValueError(
                "memory pressure threshold must not exceed the memory ceiling"
            )
        return self


class MemoryBudgetPolicy(NebulaModel):
    default_job: MemoryLimits
    aggregate_bytes: MemoryBytes
    reserve_bytes: MemoryBytes
    environment_defaults: dict[ExecutionEnvironment, MemoryLimits] = Field(
        default_factory=dict, max_length=5
    )
    profile_overrides: dict[str, MemoryLimits] = Field(
        default_factory=dict, max_length=128
    )


class MemoryBudgetUpdate(NebulaModel):
    expected_revision: int = Field(strict=True, ge=1)
    policy: MemoryBudgetPolicy


class MemoryAdministratorLimits(NebulaModel):
    capacity_bytes: MemoryBytes
    maximum_job_bytes: MemoryBytes
    maximum_aggregate_bytes: MemoryBytes
    minimum_reserve_bytes: MemoryBytes
    maximum_swap_bytes: SwapBytes = 0


class MemoryRunnerCapabilities(NebulaModel):
    protocol: Literal["nebula.memory-runner/v1"] = "nebula.memory-runner/v1"
    environment: ExecutionEnvironment
    profile_id: str | None = Field(default=None, max_length=200)
    kernel_enforced: bool = False
    protected_process_tree: bool = False
    explicit_swap: bool = False
    pressure_threshold: bool = False
    live_update: bool = False
    capacity_bytes: MemoryBytes | None = None
    detail: str


class EffectiveMemoryPolicy(NebulaModel):
    policy_revision: int = Field(ge=1)
    environment: ExecutionEnvironment
    profile_id: str | None = None
    source: Literal["default", "environment", "profile", "job"]
    limits: MemoryLimits
    administrator_limits: MemoryAdministratorLimits
    runner_protocol: Literal["nebula.memory-runner/v1"] = "nebula.memory-runner/v1"
    runner_overhead_bytes: SwapBytes = 0

    @property
    def reservation_bytes(self) -> int:
        return self.limits.reservation_bytes + self.runner_overhead_bytes


class MemoryEnforcementEvidence(NebulaModel):
    application_uncertain: bool = False
    protocol: Literal["nebula.memory-runner/v1"] = "nebula.memory-runner/v1"
    group_id: str = Field(min_length=1, max_length=300)
    job_id: str = Field(min_length=1, max_length=200)
    applied: MemoryLimits
    protected_process_tree: bool
    peak_bytes: SwapBytes | None = None
    current_bytes: SwapBytes | None = None
    observed_at: str | None = Field(default=None, max_length=50)
    oom_kill_count: int | None = Field(default=None, ge=0)
    oom_victim_confirmed: bool = False
    group_empty: bool = False
    auxiliary_group_ids: list[str] = Field(default_factory=list, max_length=8)
    auxiliary_groups_empty: bool = False
    termination_reason: MemoryTerminationReason
    detail: str = Field(max_length=4000)


class MemoryRunningUpdate(NebulaModel):
    expected_revision: int = Field(strict=True, ge=1)
    limits: MemoryLimits
    acknowledge_possible_termination: bool = Field(default=False, strict=True)


def validate_policy(
    policy: MemoryBudgetPolicy, administrator: MemoryAdministratorLimits
) -> None:
    if policy.reserve_bytes < administrator.minimum_reserve_bytes:
        raise MemoryBudgetError(
            "Core and essential services require the administrator's minimum reserve"
        )
    available = administrator.capacity_bytes - policy.reserve_bytes
    if policy.aggregate_bytes > min(available, administrator.maximum_aggregate_bytes):
        raise MemoryBudgetError(
            "aggregate budget exceeds capacity remaining after the Core reserve"
        )
    for limits in [
        policy.default_job,
        *policy.environment_defaults.values(),
        *policy.profile_overrides.values(),
    ]:
        if (
            limits.max_bytes > administrator.maximum_job_bytes
            or limits.reservation_bytes
            > min(
                administrator.capacity_bytes - policy.reserve_bytes,
                policy.aggregate_bytes,
            )
        ):
            raise MemoryBudgetError(
                "per-job ceiling exceeds the permitted job or aggregate budget"
            )
        if limits.swap_bytes > administrator.maximum_swap_bytes:
            raise MemoryBudgetError("swap allowance exceeds the administrator ceiling")


def select_memory_policy(
    policy: MemoryBudgetPolicy,
    revision: int,
    administrator: MemoryAdministratorLimits,
    capabilities: MemoryRunnerCapabilities,
    *,
    requested: MemoryLimits | None = None,
) -> EffectiveMemoryPolicy:
    validate_policy(policy, administrator)
    selected = policy.default_job
    source: Literal["default", "environment", "profile", "job"] = "default"
    if capabilities.environment in policy.environment_defaults:
        selected, source = (
            policy.environment_defaults[capabilities.environment],
            "environment",
        )
    if capabilities.profile_id in policy.profile_overrides:
        selected, source = policy.profile_overrides[capabilities.profile_id], "profile"
    if requested is not None:
        if (
            requested.max_bytes > selected.max_bytes
            or requested.swap_bytes > selected.swap_bytes
        ):
            raise MemoryBudgetError(
                "job overrides may only lower their configured profile ceiling"
            )
        if selected.high_bytes is not None and (
            requested.high_bytes is None or requested.high_bytes > selected.high_bytes
        ):
            raise MemoryBudgetError(
                "job overrides cannot remove or raise the configured pressure threshold"
            )
        selected, source = requested, "job"
    return EffectiveMemoryPolicy(
        policy_revision=revision,
        environment=capabilities.environment,
        profile_id=capabilities.profile_id,
        source=source,
        limits=selected,
        administrator_limits=administrator,
    )


def require_memory_enforcement(
    limits: MemoryLimits, capabilities: MemoryRunnerCapabilities
) -> None:
    if not capabilities.kernel_enforced or not capabilities.protected_process_tree:
        raise MemoryEnforcementUnavailable(capabilities.detail)
    if not capabilities.explicit_swap:
        raise MemoryEnforcementUnavailable(
            "This runner cannot verify an explicit swap allowance; choose a memory-managed runner"
        )
    if capabilities.capacity_bytes is None:
        raise MemoryEnforcementUnavailable(
            "This runner cannot report its memory capacity; choose a verified memory-managed runner"
        )
    if limits.high_bytes is not None and not capabilities.pressure_threshold:
        raise MemoryEnforcementUnavailable(
            "This runner cannot enforce memory.high; remove that threshold or choose a supported runner"
        )


def resolve_memory_policy(
    policy, revision, administrator, capabilities, *, requested=None
) -> EffectiveMemoryPolicy:
    selected = select_memory_policy(
        policy, revision, administrator, capabilities, requested=requested
    )
    require_memory_enforcement(selected.limits, capabilities)
    return selected


def verified_memory_outcome(evidence: MemoryEnforcementEvidence) -> str:
    if evidence.termination_reason == "memory_limit_exceeded":
        if not evidence.oom_victim_confirmed or not evidence.oom_kill_count:
            return "failed"
        return "memory_limit_exceeded"
    return evidence.termination_reason
