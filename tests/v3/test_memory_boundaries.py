"""Identity and host-capacity regressions at runner boundaries."""

import json
from types import SimpleNamespace

import pytest

from nebula.v3.memory_guard import observe_memory_group
from nebula.v3.memory_host import cgroup_memory_capacity
from nebula.v3.memory_policy import MIB, MemoryEnforcementUnavailable
from nebula.v3.automation_runtime import ContainerRuntimeSession, SessionLaunch
from nebula.v3.sandbox import SandboxLimits


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_nested_cgroup_uses_tightest_current_or_ancestor_limit(tmp_path):
    proc, mount = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    child = mount / "services/core"
    child.mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/services/core\n")
    (proc / "self/mountinfo").write_text(f"1 0 0:1 / {mount} rw - cgroup2 cgroup2 rw\n")
    (mount / "memory.max").write_text("max")
    (mount / "services/memory.max").write_text(str(128 * MIB))
    (child / "memory.max").write_text(str(256 * MIB))
    assert cgroup_memory_capacity(proc=proc, fallback=mount) == 128 * MIB


@pytest.mark.anyio
async def test_usage_proof_rejects_a_different_job_before_entering_its_group():
    class WrongJob:
        calls = []

        async def _capture(self, *arguments):
            self.calls.append(arguments)
            return (
                json.dumps(
                    {
                        "Id": "a" * 64,
                        "Config": {"Labels": {"nebula.memory.job": "other"}},
                        "State": {"Running": True},
                    }
                ),
                "",
                0,
            )

    runner = WrongJob()
    with pytest.raises(MemoryEnforcementUnavailable, match="identity"):
        await observe_memory_group(runner, "expected", "a" * 64)
    assert [call[0] for call in runner.calls] == ["inspect"]


@pytest.mark.anyio
async def test_managed_persistent_session_preserves_admitted_vpn_configuration(
    tmp_path,
):
    calls = []

    class Controller:
        async def open_persistent(self, *arguments, **options):
            calls.append(options)
            return SimpleNamespace(guard=SimpleNamespace(egress_lease=None))

    class Runner:
        memory_compute = Controller()

        async def available(self):
            return True, "fixture"

        def _validate(self, request):
            return tmp_path

    launch = SessionLaunch(
        session_id="fixture",
        project_id="project",
        runner=Runner(),
        image="fixture@sha256:" + "a" * 64,
        workspace=tmp_path,
        limits=SandboxLimits(),
        vpn_config="synthetic-private-vpn-input",
    )
    await ContainerRuntimeSession.start(launch)
    assert calls[0]["vpn_config"] == launch.vpn_config
