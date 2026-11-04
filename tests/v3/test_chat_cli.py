"""Focused terminal-client contracts; no Core process or provider calls."""

import json
import subprocess
import sys

import httpx
import pytest
from typer.testing import CliRunner

from nebula import chat_cli as cli


CORE = "https://core.example:8765"
ENTRY = {
    "device_id": "terminal-1",
    "device_token": "private-device-token",
    "csrf_token": "private-csrf",
    "device_name": "Terminal",
}
SESSION = {
    "id": "butler-1",
    "engagement_id": "project-1",
    "backend": "provider",
    "provider_profile_id": "provider-1",
    "model": "local-model",
    "metadata": {"control_assistant": True},
}
CONFIG = {
    "backend": "provider",
    "profile_id": "provider-1",
    "model": "local-model",
    "tools_enabled": False,
    "planning_enabled": False,
    "include_knowledge": False,
    "harness_cyber_access_program": "automatic",
}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "private" / "chat.json"
    cli.Settings(path, {}, CORE).save(dict(ENTRY))
    return path


def invoke(path, *args, text="", core=None):
    return CliRunner().invoke(
        cli.app,
        ["--config", str(path), *(["--core", core] if core else []), *args],
        input=text,
    )


@pytest.fixture
def transport(monkeypatch):
    original = httpx.Client

    def install(handler):
        monkeypatch.setattr(
            cli.httpx,
            "Client",
            lambda **kwargs: original(
                **kwargs, transport=httpx.MockTransport(handler), trust_env=False
            ),
        )

    return install


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://CORE.example:443/", "https://core.example"),
        ("https://core.example:8765/api/v1", CORE),
        ("https://[::1]:8765/", "https://[::1]:8765"),
    ],
)
def test_core_origin_normalization(value, expected):
    assert cli.origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "core.example",
        "ftp://core.example",
        "https://user:password@core.example",
        "https://core.example/#token=secret",
        "https://core.example/other",
        "https://core.example/?token=secret",
        "https://core.example:invalid",
    ],
)
def test_core_origin_rejects_credentials_and_non_origins(value):
    with pytest.raises(cli.ClientError):
        cli.origin(value)


def test_client_import_does_not_load_core_or_a_gui():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nebula.chat_cli; assert not any(m in sys.modules for m in ('nebula.v3', 'nebula.v3.api', 'sqlalchemy', 'chromadb', 'PyQt6', 'PySide6'))",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_pair_confirms_device_and_privately_saves_only_origin_credentials(
    tmp_path, transport
):
    path = tmp_path / "private" / "chat.json"

    def handler(req):
        assert req.headers["origin"] == CORE
        assert "authorization" not in req.headers
        if req.url.path.endswith("/redeem"):
            assert json.loads(req.content) == {
                "secret": "one-time-pairing-secret",
                "confirmation_code": "012345",
                "name": "Butler terminal",
            }
            return httpx.Response(
                200,
                json={
                    "device": {"id": ENTRY["device_id"], "name": "Terminal"},
                    "csrf_token": ENTRY["csrf_token"],
                },
                headers=[
                    (
                        "set-cookie",
                        f"nebula_device={ENTRY['device_token']}; Path=/; Secure; HttpOnly",
                    ),
                    (
                        "set-cookie",
                        f"nebula_csrf={ENTRY['csrf_token']}; Path=/; Secure",
                    ),
                ],
            )
        assert req.url.path.endswith("/devices")
        assert req.headers["x-nebula-csrf"] == ENTRY["csrf_token"]
        assert ENTRY["device_token"] in req.headers["cookie"]
        return httpx.Response(200, json=[{"id": ENTRY["device_id"], "current": True}])

    transport(handler)
    result = invoke(path, "pair", core=CORE, text="one-time-pairing-secret\n012345\n")
    assert result.exit_code == 0, result.output
    assert "Paired with" in result.output
    saved = json.loads(path.read_text())
    assert saved["cores"][CORE]["device_token"] == ENTRY["device_token"]
    assert "one-time-pairing-secret" not in path.read_text()
    assert "012345" not in path.read_text()
    assert "one-time-pairing-secret" not in result.output
    assert ENTRY["device_token"] not in result.output
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_wrong_pairing_code_preserves_retry_without_saving_credentials(
    tmp_path, transport
):
    transport(
        lambda _: httpx.Response(
            401, json={"detail": "pairing confirmation code did not match"}
        )
    )
    path = tmp_path / "chat.json"
    result = invoke(path, "pair", core=CORE, text="one-time-pairing-secret\n111111\n")
    assert result.exit_code == 1
    assert "confirmation code did not match" in result.output
    assert not path.exists()


def test_pair_requires_explicit_http_opt_in_before_collecting_secrets(tmp_path):
    result = invoke(tmp_path / "chat.json", "pair", core="http://192.0.2.7:8765")
    assert result.exit_code == 1
    assert "requires HTTPS" in result.output
    assert "Pairing secret:" not in result.output


def test_invitation_is_host_only_and_never_saves_bearer(
    tmp_path, transport, monkeypatch
):
    monkeypatch.setenv("NEBULA_V3_API_TOKEN", "host-only-bearer")
    requests = []

    def handler(req):
        requests.append(req)
        assert req.headers["authorization"] == "Bearer host-only-bearer"
        return httpx.Response(
            200,
            json={
                "secret": "invitation",
                "confirmation_code": "012345",
                "expires_at": "soon",
            },
        )

    transport(handler)
    path = tmp_path / "chat.json"
    remote = invoke(path, "invite", core=CORE)
    assert remote.exit_code == 1
    assert not requests
    local = invoke(path, "invite", core="http://127.0.0.1:8765")
    assert local.exit_code == 0, local.output
    assert "invitation" in local.output
    assert "host-only-bearer" not in local.output
    assert not path.exists()


def test_credentials_are_never_reused_for_another_core(config, transport):
    transport(lambda _: pytest.fail("A different origin must require a new pairing"))
    result = invoke(config, "projects", core="https://other.example")
    assert result.exit_code == 1
    assert "not paired" in result.output


@pytest.mark.parametrize(
    ("status", "message"), [(401, "expired or was revoked"), (302, "redirected")]
)
def test_auth_and_redirect_failures_are_actionable(config, transport, status, message):
    transport(
        lambda _: httpx.Response(
            status,
            headers={"Location": "https://other.example"},
            json={"detail": "rejected"},
        )
    )
    result = invoke(config, "projects")
    assert result.exit_code == 1
    assert message in result.output
    assert ENTRY["device_token"] not in result.output


@pytest.mark.parametrize("status", [204, 401])
def test_unpair_revokes_or_removes_already_rejected_credentials(
    config, transport, status
):
    def handler(req):
        assert req.method == "DELETE"
        assert req.url.path.endswith("/auth/devices/terminal-1")
        assert req.headers["origin"] == CORE
        assert req.headers["x-nebula-csrf"] == ENTRY["csrf_token"]
        return httpx.Response(status)

    transport(handler)
    assert invoke(config, "unpair").exit_code == 0
    assert CORE not in json.loads(config.read_text())["cores"]


def test_network_failure_keeps_pairing_for_revocation_retry(config, transport):
    def handler(req):
        raise httpx.ConnectError("offline", request=req)

    transport(handler)
    result = invoke(config, "unpair")
    assert result.exit_code == 1
    assert "Check its address" in result.output
    assert CORE in json.loads(config.read_text())["cores"]


def test_projects_paginate_and_create_reads_authoritative_record(config, transport):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.url.params.get("offset")))
        if req.method == "POST":
            return httpx.Response(201, json={"id": "new", "name": "Requested"})
        if req.url.path.endswith("/new"):
            return httpx.Response(200, json={"id": "new", "name": "Saved"})
        offset = int(req.url.params.get("offset", "0"))
        return httpx.Response(
            200,
            json=[
                {"id": str(i), "name": f"Project {i}"}
                for i in range(offset, min(offset + 100, 101))
            ],
        )

    transport(handler)
    result = invoke(config, "projects", "--create", "Requested")
    assert result.exit_code == 0, result.output
    assert "Created Saved" in result.output
    assert "Project 100" in result.output
    assert ("GET", "/api/v1/engagements", "100") in calls


def chat_handler(req):
    path = req.url.path
    assert "authorization" not in req.headers
    if path.endswith("/engagements"):
        return httpx.Response(200, json=[{"id": "project-1", "name": "Project"}])
    if path.endswith("/control-assistant/session"):
        return httpx.Response(200, json=SESSION)
    if path.endswith("/messages"):
        return httpx.Response(
            200, json=[{"role": "assistant", "content": "Saved previous reply"}]
        )
    if path.endswith("/pending-turn"):
        return httpx.Response(200, json=None)
    if path.endswith("/assistant-config"):
        return httpx.Response(200, json={"settings": CONFIG})
    pytest.fail(f"Unexpected request: {req.method} {req.url}")


def test_chat_resumes_saved_session_streams_and_sends_only_the_new_message(
    config, transport
):
    submitted = []

    def handler(req):
        if req.url.path.endswith("/completions"):
            body = json.loads(req.content)
            submitted.append(body)
            return httpx.Response(
                200,
                text=': heartbeat\n\nevent: started\ndata: {"turn_id":"turn-1"}\n\nevent: delta\ndata: {"delta":"Hello "}\n\nevent: done\ndata: {"message":{"content":"Hello terminal"}}\n\n',
            )
        return chat_handler(req)

    transport(handler)
    result = invoke(
        config, "chat", "--project", "Project", text="new question\n/quit\n"
    )
    assert result.exit_code == 0, result.output
    assert "Saved previous reply" in result.output
    assert "Hello terminal" in result.output
    assert len(submitted) == 1
    assert submitted[0]["control_assistant"] is True
    assert submitted[0]["session_id"] == "butler-1"
    assert submitted[0]["messages"] == [{"role": "user", "content": "new question"}]
    assert "harness_cyber_access_program" not in submitted[0]


def test_disconnected_stream_is_not_reported_complete_or_replayed(config, transport):
    submitted = []

    def handler(req):
        if req.url.path.endswith("/completions"):
            submitted.append(req)
            return httpx.Response(
                200, text='event: delta\ndata: {"delta":"Partial"}\n\n'
            )
        return chat_handler(req)

    transport(handler)
    result = invoke(config, "chat", text="question\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "ended before completion" in result.output
    assert len(submitted) == 1


def test_existing_turn_can_be_followed_and_stopped_without_resubmission(
    config, transport
):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/pending-turn"):
            return httpx.Response(200, json={"id": "turn-1", "status": "running"})
        if req.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text='event: done\ndata: {"message":{"content":"Existing answer"}}\n\n',
            )
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        return chat_handler(req)

    transport(handler)
    result = invoke(config, "chat", text="another question\n/follow\n/stop\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "already active" in result.output
    assert "Existing answer" in result.output
    assert "cancelled" in result.output
    assert not any(path.endswith("/completions") for _, path in calls)


def test_stream_keyboard_interrupt_cancels_the_exact_known_turn(monkeypatch):
    calls = []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'event: started\ndata: {"turn_id":"exact-turn"}\n\n'
            raise KeyboardInterrupt

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        return httpx.Response(200, stream=Interrupted())

    with httpx.Client(
        base_url=CORE + "/api/v1/", transport=httpx.MockTransport(handler)
    ) as client:
        cli.stream(client, method="POST", path="chat/completions", body={})
    assert calls == ["/api/v1/chat/completions", "/api/v1/chat/turns/exact-turn/cancel"]


def test_ambiguous_project_names_require_a_number(config, transport):
    def handler(req):
        if req.url.path.endswith("/engagements"):
            return httpx.Response(
                200,
                json=[
                    {"id": "other", "name": "Project"},
                    {"id": "project-1", "name": "Project"},
                ],
            )
        return chat_handler(req)

    transport(handler)
    result = invoke(
        config, "chat", "--project", "Project", text="0\n999\ninvalid\n2\n/quit\n"
    )
    assert result.exit_code == 0, result.output
    assert "Choose project" in result.output


def test_new_butler_uses_project_runtime_default(config, transport):
    reservations = []

    def handler(req):
        if req.url.path.endswith("/engagements"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "project-1",
                        "name": "Project",
                        "assistant_defaults": {
                            "backend": "provider",
                            "provider_id": "provider-1",
                            "model": "preferred-model",
                        },
                    }
                ],
            )
        if req.url.path.endswith("/control-assistant/session"):
            if req.method == "GET":
                return httpx.Response(200, json=None)
            reservations.append(json.loads(req.content))
            return httpx.Response(200, json=SESSION)
        if req.url.path.endswith("/providers"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "provider-1",
                        "name": "Local",
                        "enabled": True,
                        "model_allowlist": ["other-model", "preferred-model"],
                    }
                ],
            )
        if req.url.path.endswith("/harnesses"):
            return httpx.Response(200, json=[])
        return chat_handler(req)

    transport(handler)
    result = invoke(config, "chat", text="/quit\n")
    assert result.exit_code == 0, result.output
    assert reservations == [
        {
            "engagement_id": "project-1",
            "backend": "provider",
            "model": "preferred-model",
            "provider_id": "provider-1",
        }
    ]


def test_terminal_output_removes_escape_sequences_and_controls():
    assert (
        cli.terminal_text("Hi\x1b[31m red\x1b[0m\x1b]52;c;payload\x07\x08\nthere")
        == "Hi red\nthere"
    )


@pytest.mark.parametrize(
    ("event", "status"),
    [("approval", "waiting_approval"), ("interaction", "waiting_input")],
)
def test_harness_waits_return_to_prompt_with_visible_action(
    config, transport, event, status
):
    def handler(req):
        if req.url.path.endswith("/completions"):
            payload = {
                "type": event,
                "item_status": status,
                "summary": "An operator decision is needed",
            }
            return httpx.Response(
                200, text=f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            )
        return chat_handler(req)

    transport(handler)
    result = invoke(config, "chat", text="question\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Action required: An operator decision is needed" in result.output
    assert "ended before completion" not in result.output


def test_harness_saved_runtime_controls_are_sent_without_loss():
    settings = {
        **CONFIG,
        "backend": "harness",
        "profile_id": "harness-1",
        "harness_cyber_access_program": "standard",
    }
    with httpx.Client(
        base_url=CORE + "/api/v1/",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"settings": settings})
        ),
    ) as client:
        body = cli.turn_body(client, "project-1", SESSION, "Hello")
    assert body["harness_profile_id"] == "harness-1"
    assert body["harness_cyber_access_program"] == "standard"
    assert "provider_id" not in body


def test_advertised_harness_model_and_stale_project_default_fall_back(
    config, transport
):
    reservations = []

    def handler(req):
        if req.url.path.endswith("/engagements"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "project-1",
                        "name": "Project",
                        "assistant_defaults": {
                            "backend": "harness",
                            "harness_id": "deleted-harness",
                        },
                    }
                ],
            )
        if req.url.path.endswith("/control-assistant/session"):
            if req.method == "GET":
                return httpx.Response(200, json=None)
            reservations.append(json.loads(req.content))
            return httpx.Response(200, json=SESSION)
        if req.url.path.endswith("/providers"):
            return httpx.Response(200, json=[])
        if req.url.path.endswith("/harnesses"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "ready-harness",
                        "name": "Ready harness",
                        "enabled": True,
                        "capabilities": {"models": ["advertised-model"]},
                    }
                ],
            )
        return chat_handler(req)

    transport(handler)
    result = invoke(config, "chat", text="/quit\n")
    assert result.exit_code == 0, result.output
    assert reservations[0]["harness_profile_id"] == "ready-harness"
    assert reservations[0]["model"] == "advertised-model"


def test_headless_invokes_core_without_frontend_browser_or_desktop(
    tmp_path, monkeypatch
):
    from nebula.v3 import cli as core_cli

    observed = {}
    monkeypatch.setattr(core_cli, "serve", lambda **kwargs: observed.update(kwargs))
    result = CliRunner().invoke(core_cli.app, ["headless", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert observed["static_dir"] is None
    assert observed["open_browser"] is False
    assert observed["allow_unauthenticated_remote"] is False
    assert observed["handshake_stdout"] is False


@pytest.mark.parametrize(
    "args", [[], ["--allow-remote"], ["--tls-cert", "cert.pem", "--tls-key", "key.pem"]]
)
def test_remote_headless_requires_acknowledgement_and_tls(args, monkeypatch):
    from nebula.v3 import cli as core_cli

    monkeypatch.setattr(
        core_cli,
        "serve",
        lambda **_: pytest.fail("Invalid remote configuration must not start Core"),
    )
    result = CliRunner().invoke(core_cli.app, ["headless", "--host", "0.0.0.0", *args])
    assert result.exit_code == 2
    assert "requires" in result.output


def test_core_exposes_the_same_terminal_commands():
    from nebula.v3.cli import app

    result = CliRunner().invoke(app, ["chat", "--help"])
    assert result.exit_code == 0
    for name in ("invite", "pair", "projects", "chat", "unpair"):
        assert name in result.output
