"""The managed harness must retain Git's trusted CA without copying ambient secrets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import HarnessKind, HarnessProfile
from nebula.v3.harnesses import CodexAppServerAdapter, _CodexRpc


def test_codex_spawn_keeps_git_ca_for_worker_commands(tmp_path, monkeypatch):
    ca_file = tmp_path / "mercury-ca.pem"
    ca_file.write_text("synthetic CA", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("GIT_SSL_CAINFO", str(ca_file))
    monkeypatch.setenv("NEBULA_TEST_SECRET_SENTINEL", "private")

    captured_envs = []

    async def fake_spawn(*_argv, **kwargs):
        captured_envs.append(kwargs["env"])
        return SimpleNamespace(stdin=None, stdout=None, stderr=None)

    async def fake_start(_rpc):
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(_CodexRpc, "start", fake_start)
    executable = tmp_path / "codex"
    executable.touch()
    profile = HarnessProfile(
        name="codex",
        kind=HarnessKind.CODEX_APP_SERVER,
        executable=str(executable),
    )

    asyncio.run(
        CodexAppServerAdapter()._connect(profile, CredentialStore(), (), tmp_path)
    )

    assert len(captured_envs) == 1
    child_env = captured_envs[0]
    assert child_env["GIT_SSL_CAINFO"] == str(ca_file)
    assert child_env["SSL_CERT_FILE"] == str(ca_file)
    assert "NEBULA_TEST_SECRET_SENTINEL" not in child_env
