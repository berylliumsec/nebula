"""Native-channel readback rejects unsupported and uncertain receipts."""

from tests.v3.async_test_support import anyio_backend as anyio_backend

import os
import pytest
from nebula.v3.harness_instructions import grok_channel_is_confirmed


@pytest.mark.parametrize(
    "state",
    [
        "valid",
        "missing",
        "stale",
        "duplicate_text",
        "duplicate_session",
        "file_link",
        "folder_link",
        "fifo",
        "bad_utf8",
    ],
)
def test_grok_readback_requires_one_fresh_regular_native_head(tmp_path, state):
    home = tmp_path / "account"
    path = home / "sessions" / "project" / "native-id" / "system_prompt.txt"
    path.parent.mkdir(parents=True)
    expected = "Exact synthetic rules\r\nUnicode 🐈\n"
    path.write_text("Native base\n" + expected)
    not_before = 1
    if state == "missing":
        path.unlink()
    elif state == "stale":
        os.utime(path, ns=(1, 1))
        not_before = 2
    elif state == "duplicate_text":
        path.write_text(expected + expected)
    elif state == "duplicate_session":
        second = home / "sessions" / "other-project" / "native-id" / "system_prompt.txt"
        second.parent.mkdir(parents=True)
        second.write_text(expected)
    elif state == "file_link":
        target = tmp_path / "outside.txt"
        target.write_text(expected)
        path.unlink()
        path.symlink_to(target)
    elif state == "folder_link":
        original = path.parent
        original.rename(tmp_path / "outside-session")
        original.symlink_to(tmp_path / "outside-session", target_is_directory=True)
    elif state == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif state == "bad_utf8":
        path.write_bytes(b"\xff")
    assert grok_channel_is_confirmed(
        home, "native-id", expected, not_before_ns=not_before
    ) is (state == "valid")


@pytest.mark.parametrize("external_id", ["../outside", "native/id", "", "a" * 201])
def test_grok_readback_rejects_unsafe_external_identity(tmp_path, external_id):
    assert not grok_channel_is_confirmed(
        tmp_path, external_id, "rules", not_before_ns=0
    )


async def test_managed_acp_readback_uses_a_cold_local_process(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import nebula.v3.harnesses as module
    from nebula.v3.domain import HarnessKind, HarnessProfile, HarnessSession
    from nebula.v3.harness_instructions import instruction_snapshot

    calls = []

    async def spawn(*command, **kwargs):
        calls.append(command)
        return SimpleNamespace()

    async def start(_rpc):
        pass

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module._AcpRpc, "start", start)
    profile = HarnessProfile(
        name="Synthetic ACP", kind=HarnessKind.GROK_ACP, executable="/bin/true"
    )
    session = HarnessSession(
        engagement_id="synthetic-project",
        harness_profile_id=profile.id,
        model="test-model",
        metadata={"standing_instructions": instruction_snapshot("Rules")},
    )
    await module.GrokAcpAdapter()._connect(profile, tmp_path, session)
    assert calls[0].count("--no-leader") == 1
    assert (
        calls[0].index("agent")
        < calls[0].index("--no-leader")
        < calls[0].index("stdio")
    )


pytestmark = pytest.mark.anyio
