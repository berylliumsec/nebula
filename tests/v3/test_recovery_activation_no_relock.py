"""A verified recovery that is still activating is not re-checked under lock.

While a restored worker activates, the supervisor's handoff loop re-selects
the verified recovery every pass until the worker reports ready. Re-checking
blockers then held the project's ownership row FOR UPDATE through a
project-wide scan and starved the activating worker (live: ~18 minutes).
"""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from nebula.v3.runtime_coordination import ProjectLease
from nebula.v3.supervisor import RuntimeSupervisor

LEASE = ProjectLease("project", "target", 2)


class FakeSupervisor:
    _advance_project_recovery = RuntimeSupervisor._advance_project_recovery

    def __init__(self, *, phase, state, activation_started):
        self.phase = phase
        self.state = state
        self.activation_started = activation_started
        self.grant_checks = []
        self.verified = 0
        self.activated = 0
        self._activated_handoffs = set()
        self.coordinator = SimpleNamespace(verify_recovery=self._verify)

    async def _recovery_grant(self, row, *, check_blockers=False):
        self.grant_checks.append(check_blockers)
        return LEASE, "operation", self.phase, "digest"

    async def _recovery_status(self, lease):
        return {
            "lease": asdict(lease),
            "recovery_operation_id": "operation",
            "restored_checkpoint_digest": "digest",
            "state": self.state,
            "activation_started": self.activation_started,
        }

    async def _verify(self, lease, **_):
        self.verified += 1
        self.phase = "verified"

    async def activate_execution(self, lease):
        self.activated += 1
        self.state = "ready"


@pytest.mark.asyncio
async def test_activating_verified_recovery_skips_blockers_and_activation():
    supervisor = FakeSupervisor(
        phase="verified", state="prepared", activation_started=True
    )
    for _ in range(3):
        assert await supervisor._advance_project_recovery(object()) == LEASE
    assert supervisor.grant_checks == [False] * 6
    assert supervisor.verified == supervisor.activated == 0
    # Still activating: the loop keeps watching until the worker is ready.
    assert supervisor._activated_handoffs == set()

    supervisor.state = "ready"
    await supervisor._advance_project_recovery(object())
    assert supervisor._activated_handoffs == {"target"}


@pytest.mark.asyncio
async def test_unverified_recovery_still_checks_blockers_before_verifying():
    supervisor = FakeSupervisor(
        phase="prepared", state="prepared", activation_started=False
    )
    await supervisor._advance_project_recovery(object())
    assert supervisor.grant_checks == [False, True]
    assert supervisor.verified == supervisor.activated == 1
    assert supervisor._activated_handoffs == {"target"}


@pytest.mark.asyncio
async def test_verified_but_never_activated_recovery_checks_blockers_then_activates():
    # For example a supervisor restart between verification and activation:
    # an effect that became uncertain meanwhile must still hold activation.
    supervisor = FakeSupervisor(
        phase="verified", state="prepared", activation_started=False
    )
    await supervisor._advance_project_recovery(object())
    assert supervisor.grant_checks == [False, True]
    assert supervisor.activated == 1
    assert supervisor._activated_handoffs == {"target"}
