"""Deployment connection budgets. Observed idle connections are not a budget."""

from __future__ import annotations

import os
import math
from dataclasses import asdict, dataclass, replace
from typing import Mapping


@dataclass(frozen=True)
class PoolSettings:
    admin_reserve: int
    other_connections: int
    peak_processes: int
    requested_size: int | None = None
    timeout: float = 30.0

    def auxiliary(self) -> PoolSettings:
        """A serial CLI client consumes one declared non-Core connection."""
        if self.other_connections < 1:
            raise ValueError(
                "NEBULA_V3_DB_OTHER_CONNECTIONS must reserve at least one "
                "connection for this maintenance/CLI process"
            )
        return replace(self, requested_size=1)

    def __post_init__(self) -> None:
        if (
            self.admin_reserve < 1
            or self.other_connections < 0
            or self.peak_processes < 1
        ):
            raise ValueError("invalid PostgreSQL connection budget")
        if self.requested_size is not None and self.requested_size < 1:
            raise ValueError("pool size must be a positive finite connection count")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("pool timeout must be positive and finite")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> PoolSettings:
        environment = os.environ if environment is None else environment

        def integer(name: str, minimum: int) -> int:
            key = "NEBULA_V3_DB_" + name
            raw = environment.get(key)
            if raw is None:
                raise ValueError(
                    f"{key} is required for PostgreSQL connection budgeting"
                )
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{key} must be an integer") from exc
            if value < minimum:
                raise ValueError(f"{key} must be at least {minimum}")
            return value

        size = (
            integer("POOL_SIZE", 1) if "NEBULA_V3_DB_POOL_SIZE" in environment else None
        )
        timeout = float(environment.get("NEBULA_V3_DB_POOL_TIMEOUT", "30"))
        if not 0 < timeout < float("inf"):
            raise ValueError("NEBULA_V3_DB_POOL_TIMEOUT must be positive and finite")
        return cls(
            admin_reserve=integer("ADMIN_RESERVE", 1),
            other_connections=integer("OTHER_CONNECTIONS", 0),
            peak_processes=integer("PEAK_PROCESSES", 1),
            requested_size=size,
            timeout=timeout,
        )


@dataclass(frozen=True)
class ConnectionBudget:
    max_connections: int
    postgres_reserved: int
    admin_reserve: int
    other_connections: int
    peak_processes: int
    role_limit: int
    database_limit: int
    ceiling: int
    pool_size: int

    def snapshot(self) -> dict[str, int]:
        return asdict(self)


def connection_budget(
    settings: PoolSettings,
    *,
    max_connections: int,
    postgres_reserved: int,
    role_limit: int = -1,
    database_limit: int = -1,
) -> ConnectionBudget:
    """Bound the fleet, conservatively charging other consumers to each limit.

    OTHER_CONNECTIONS includes maintenance, probes, and every pool outside this
    fleet. PEAK_PROCESSES includes rolling-deployment overlap, not just steady
    replicas. Administrators must keep those declared allocations authoritative.
    """
    if (
        settings.admin_reserve < 1
        or settings.other_connections < 0
        or settings.peak_processes < 1
        or postgres_reserved < 0
    ):
        raise ValueError("invalid PostgreSQL connection budget")
    available = (
        max_connections
        - postgres_reserved
        - settings.admin_reserve
        - settings.other_connections
    )
    for limit in (role_limit, database_limit):
        if limit >= 0:
            available = min(
                available, limit - settings.admin_reserve - settings.other_connections
            )
    ceiling = available // settings.peak_processes
    if ceiling < 1:
        raise ValueError(
            "PostgreSQL connection budget leaves no connection per Core process"
        )
    size = ceiling if settings.requested_size is None else settings.requested_size
    if not 1 <= size <= ceiling:
        raise ValueError(
            f"PostgreSQL pool size must be between 1 and the safe ceiling {ceiling}"
        )
    return ConnectionBudget(
        max_connections,
        postgres_reserved,
        settings.admin_reserve,
        settings.other_connections,
        settings.peak_processes,
        role_limit,
        database_limit,
        ceiling,
        size,
    )
