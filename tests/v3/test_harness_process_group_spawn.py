"""Harness CLIs start in their own process group on every supported loop."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
import uvloop

from nebula.v3.harnesses import _own_process_group_spawn_options

_PROBE = "import os; print(os.getpgid(0), os.getsid(0), flush=True)"


async def _spawn_and_probe() -> tuple[int, int, int]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PROBE,
        stdout=asyncio.subprocess.PIPE,
        **_own_process_group_spawn_options(),
    )
    out, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    group, session = (int(item) for item in out.split())
    return process.pid, group, session


@pytest.mark.parametrize("runner", [asyncio.run, uvloop.run], ids=["asyncio", "uvloop"])
def test_harness_child_gets_its_own_group_in_the_worker_session(runner):
    pid, group, session = runner(_spawn_and_probe())
    assert group == pid
    assert group != os.getpgid(0)
    assert session == os.getsid(0)
