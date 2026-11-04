"""Real Core with deterministic available host capacity for lifecycle journeys.

These inert-provider journeys exercise replies, history, and browser lifecycle.
Host pressure and start-budget behavior have dedicated production-gate coverage.
Keep normal CLI serving, pairing, authorization, persistence, and scheduling.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI

from nebula.v3 import cli
from nebula.v3.api import create_app
from nebula.v3.host_load import HostLoadGate, HostLoadMonitor, HostLoadSnapshot


class AvailableHostLoadMonitor(HostLoadMonitor):
    def cached(self) -> HostLoadSnapshot:
        return self.sample()

    def sample(self) -> HostLoadSnapshot:
        return HostLoadSnapshot(
            sampled_at=time.monotonic(),
            cpu_capacity=4,
            cpu_ratio=0.0,
            memory_available_ratio=1.0,
            busy=False,
            reason=None,
            measurements_available=True,
        )


def create_available_host_app(*args: Any, **kwargs: Any) -> FastAPI:
    return create_app(
        *args, **kwargs, host_load_gate=HostLoadGate(AvailableHostLoadMonitor())
    )


if __name__ == "__main__":
    cli.create_app = create_available_host_app
    cli.main()
