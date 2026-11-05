"""Real paired TLS LAN Core, CLI subprocesses, and inert provider/harness work."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pyte
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from keyring.backends.null import Keyring
from PIL import Image

from nebula.v3 import chat as chat_module
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.credentials import CredentialStore
from nebula.v3.domain import (
    ChatSession,
    Engagement,
    HarnessCapabilities,
    HarnessInteractionKind,
    HarnessProfile,
    McpApprovalMode,
    McpCapabilitySnapshot,
    McpServerProfile,
    McpToolSnapshot,
    McpTransport,
    ProviderProfile,
    utc_now,
)
from nebula.v3.harnesses import (
    ADAPTER_CONTRACT_VERSION,
    HarnessAdapter,
    HarnessConnection,
    HarnessEvent,
    HarnessHealth,
    HarnessInteractionRequest,
    HarnessPermissionRequest,
    HarnessRuntimeService,
)
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.providers import (
    ModelResponse,
    ModelStreamEvent,
    ModelUsage,
    StreamEventType,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.workspace import WorkspaceService
from tests.v3.test_chat_api import ApiChatProvider
from tests.v3.test_knowledge_index import SecurityEmbeddingFunction


class Provider(ApiChatProvider):
    def __init__(self, provider_id):
        super().__init__(provider_id)
        self.holding = threading.Event()

    async def complete(self, request):
        current = next(
            (item for item in reversed(request.messages) if item.role == "user"), None
        )
        if current is not None and "hold response" in str(current.content):
            self.holding.set()
            await asyncio.Event().wait()
        answer = (
            "**Synthetic provider answer.**\n\n"
            "| Goal | State |\n|---|---|\n| HEIF | Paused |\n\n"
            "[parent](/projects/terminal-project/workbench?view=chat&session=synthetic)"
            if current is not None and "markdown layout" in str(current.content)
            else "Synthetic provider answer."
        )
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model-a",
            text=answer,
            usage=ModelUsage(input_tokens=5, output_tokens=4, total_tokens=9),
            finish_reason="stop",
        )

    async def stream(self, request):
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        current = next(
            (item for item in reversed(request.messages) if item.role == "user"), None
        )
        if current is not None and "markdown layout" in str(current.content):
            result = await self.complete(request)
            midpoint = len(result.text) // 2
            yield ModelStreamEvent(
                type=StreamEventType.TEXT_DELTA, delta=result.text[:midpoint]
            )
            yield ModelStreamEvent(
                type=StreamEventType.TEXT_DELTA, delta=result.text[midpoint:]
            )
            yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=result)
            return
        yield ModelStreamEvent(
            type=StreamEventType.TEXT_DELTA, delta="Synthetic provider "
        )
        result = await self.complete(request)
        yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta="answer.")
        yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=result)


class Connection(HarnessConnection):
    adapter_version = ADAPTER_CONTRACT_VERSION + "/terminal-fixture"

    def __init__(self, request):
        self.request = request
        self.external_session_id = "fixture-" + request.session.id

    async def run_turn(self, prompt, *, model, **options):
        yield HarnessEvent(type="started", external_session_id=self.external_session_id)
        if "ask fixture question" in prompt:
            _identity, answer = await self.request.interaction_handler(
                HarnessInteractionRequest(
                    vendor_request_id=str(uuid4()),
                    kind=HarnessInteractionKind.USER_INPUT,
                    prompt="Synthetic terminal question",
                    questions=[
                        {
                            "id": "fixture-answer",
                            "question": "Choose a fixture answer",
                            "options": ["continue", "stop"],
                        }
                    ],
                )
            )
            await answer
        if "request fixture approval" in prompt:
            ticket = await self.request.permission_handler(
                HarnessPermissionRequest(
                    vendor_request_id=str(uuid4()),
                    category="mcp",
                    vendor_name="mcp__fixture__confirm",
                    server_name="fixture",
                    tool_name="confirm",
                    arguments={"record": "synthetic"},
                    rationale="Synthetic review only; the fixture never writes a file",
                )
            )
            await ticket.decision
            ticket.acknowledge_handoff("sent")
        yield HarnessEvent(type="message_delta", delta="Synthetic harness answer.")
        yield HarnessEvent(type="completed", message="Synthetic harness answer.")

    async def steer(self, text):
        pass

    async def interrupt(self):
        pass

    async def close(self):
        pass


class Adapter(HarnessAdapter):
    async def open(self, request):
        return Connection(request)

    async def probe(self, profile, credential_store):
        return HarnessHealth(
            profile_id=profile.id,
            healthy=True,
            kind=profile.kind,
            capabilities=profile.capabilities,
        )


class TerminalProcess:
    def __init__(self, config, env, *args, tty=False):
        command = [
            sys.executable,
            "-u",
            "-m",
            "nebula.chat_cli",
            "--config",
            str(config),
            *args,
        ]
        self.master = None
        self.screen = pyte.Screen(80, 24) if tty else None
        self.screen_stream = pyte.Stream(self.screen) if self.screen else None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace") if tty else None
        if tty:
            import fcntl
            import pty
            import struct
            import termios

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            self.master = master
            self.process = subprocess.Popen(
                command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env={
                    **env,
                    "NEBULA_CHAT_PLAIN": "0",
                    "TERM": "xterm-256color",
                    "PROMPT_TOOLKIT_NO_CPR": "1",
                },
            )
            os.close(slave)
            self.input_fd = self.output_fd = master
        else:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
            )
            self.input_fd = self.process.stdin.fileno()
            self.output_fd = self.process.stdout.fileno()
        self.output = bytearray()
        self.position = 0

    def expect(self, marker, timeout=25):
        target = marker.encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.output.find(target, self.position)
            if found >= 0:
                self.position = found + len(target)
                return
            if self.screen is not None and marker in "\n".join(self.screen.display):
                self.position = len(self.output)
                return
            ready, _, _ = select.select([self.output_fd], [], [], 0.05)
            if ready:
                data = os.read(self.output_fd, 65536)
                if not data:
                    break
                self.output.extend(data)
                if self.screen_stream is not None and self.decoder is not None:
                    self.screen_stream.feed(self.decoder.decode(data))
        pytest.fail(
            f"Terminal did not show {marker!r}:\n{self.output.decode(errors='replace')[-6000:]}"
        )

    def expect_screen(self, *markers, absent=(), timeout=25):
        assert self.screen is not None and self.screen_stream is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            display = "\n".join(self.screen.display)
            if all(marker in display for marker in markers) and all(
                marker not in display for marker in absent
            ):
                return display
            ready, _, _ = select.select([self.output_fd], [], [], 0.05)
            if ready:
                data = os.read(self.output_fd, 65536)
                if not data:
                    break
                self.output.extend(data)
                assert self.decoder is not None
                self.screen_stream.feed(self.decoder.decode(data))
        pytest.fail(f"Terminal screen did not settle: {markers!r}; {display}")

    def send(self, text, expect=None):
        os.write(
            self.input_fd, (text + ("\r" if self.master is not None else "\n")).encode()
        )
        if expect:
            self.expect(expect)

    def close(self):
        if self.process.poll() is None:
            self.send("/quit")
            if self.master is not None:
                deadline = time.monotonic() + 10
                while self.process.poll() is None and time.monotonic() < deadline:
                    ready, _, _ = select.select([self.master], [], [], 0.05)
                    if ready:
                        try:
                            data = os.read(self.master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        self.output.extend(data)
                        if self.screen_stream is not None and self.decoder is not None:
                            self.screen_stream.feed(self.decoder.decode(data))
            try:
                self.process.wait(timeout=1 if self.master is not None else 10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
        assert self.process.returncode == 0, self.output.decode(errors="replace")
        if self.master is not None:
            os.close(self.master)
            self.master = None
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None and not pipe.closed:
                pipe.close()


@pytest.fixture
def lan_core(tmp_path, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        pytest.skip("A non-loopback LAN interface is required")
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic terminal Core")]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address(address)),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private, hashes.SHA256())
    )
    certificate, key = tmp_path / "core.pem", tmp_path / "core-key.pem"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("Synthetic project notes.\n")
    skill = workspace / ".agents" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: Summarize the synthetic document.\n---\nRead the synthetic notes and return a concise summary.\n"
    )
    store = NebulaStore(tmp_path / "core.db")
    project = store.create(
        Engagement(
            id="terminal-project",
            name="Terminal Project",
            workspace_path=str(workspace),
            assistant_defaults={
                "backend": "provider",
                "provider_id": "provider-a",
                "model": "model-a",
            },
        )
    )
    store.create(
        ProviderProfile(
            id="provider-a",
            name="Synthetic provider",
            provider_type="vllm",
            is_local=True,
            model_allowlist=["model-a"],
            metadata={"options": {"context_window": 32768, "max_output_tokens": 2048}},
            capabilities={"streaming": True, "tool_calling": True, "vision": True},
            capability_verifications={
                "model-a": {"model": "model-a", "status": "verified"}
            },
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    profile = store.create(
        HarnessProfile(
            id="harness-a",
            name="Synthetic harness",
            kind="codex_app_server",
            executable="/bin/true",
            default_model="model-a",
            capabilities=HarnessCapabilities(
                models=["model-a"],
                mcp=True,
                interruption=True,
                steering=True,
                interactions=True,
                checked_at=utc_now(),
            ),
            privacy={"local_only": True, "permits_sensitive_data": True},
        )
    )
    mcp = store.create(
        McpServerProfile(
            id="fixture-mcp",
            name="fixture",
            enabled=True,
            transport=McpTransport.STREAMABLE_HTTP,
            url="https://fixture.invalid/mcp",
            default_approval=McpApprovalMode.ASK,
            capabilities=McpCapabilitySnapshot(
                checked_at=utc_now(),
                tools=[
                    McpToolSnapshot(
                        name="confirm",
                        input_schema={
                            "type": "object",
                            "properties": {"record": {"type": "string"}},
                            "required": ["record"],
                        },
                        read_only=True,
                        destructive=False,
                        idempotent=True,
                        open_world=False,
                        credentialed=False,
                        annotations_complete=True,
                    )
                ],
            ),
        )
    )
    credentials = CredentialStore(keyring_backend=Keyring())
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = HarnessRuntimeService(
        store,
        credential_store=credentials,
        artifact_store=artifacts,
        workspace_resolver=lambda _: workspace,
        adapter_factory=lambda _: Adapter(),
    )
    harness_session = runtime.create_session(
        engagement_id=project.id,
        profile_id=profile.id,
        model="model-a",
        tools_enabled=False,
        mcp_server_ids=[mcp.id],
    )
    store.create(
        ChatSession(
            id="harness-chat",
            title="Harness chat",
            engagement_id=project.id,
            backend="harness",
            model="model-a",
            harness_profile_id=profile.id,
            harness_session_id=harness_session.id,
            metadata={
                "initial_title_state": "operator",
                "include_knowledge": False,
                "mcp_server_ids": [mcp.id],
            },
        )
    )
    provider = Provider("provider-a")
    monkeypatch.setattr(chat_module, "provider_from_profile", lambda profile: provider)
    knowledge = ChromaKnowledgeIndex(
        tmp_path / "index", embedding_function=SecurityEmbeddingFunction()
    )

    class FixtureWorkspace:
        def workspace_for(self, engagement_id):
            assert engagement_id == project.id
            return workspace

    workspaces = WorkspaceService(
        store=store, artifact_store=artifacts, tool_platform=FixtureWorkspace()
    )
    app = create_app(
        store,
        auth_token="synthetic-terminal-admin",
        credential_store=credentials,
        artifact_store=artifacts,
        harness_runtime_service=runtime,
        knowledge_index=knowledge,
        execution_data_root=tmp_path,
        workspace_service=workspaces,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"https://{address}:{port}"
    admin = httpx.Client(
        base_url=f"https://127.0.0.1:{port}/api/v1/",
        verify=ssl.create_default_context(cafile=str(certificate)),
        trust_env=False,
        headers={"Authorization": "Bearer synthetic-terminal-admin"},
        timeout=30,
    )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            if admin.get("health").is_success:
                break
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    else:
        pytest.fail("Synthetic LAN Core did not start")
    config = tmp_path / "client" / "chat.json"
    env = {
        **os.environ,
        "NEBULA_CORE_URL": origin,
        "NEBULA_CHAT_PLAIN": "1",
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": ",".join(
            filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1", address])
        ),
    }
    env["no_proxy"] = env["NO_PROXY"]
    invitation = admin.post("auth/pairings", json={"name": "Terminal fixture"}).json()
    paired = subprocess.run(
        [
            sys.executable,
            "-m",
            "nebula.chat_cli",
            "--config",
            str(config),
            "--core",
            origin,
            "pair",
            "--ca-cert",
            str(certificate),
        ],
        input=invitation["secret"] + "\n" + invitation["confirmation_code"] + "\n",
        check=False,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert paired.returncode == 0, paired.stdout + paired.stderr
    processes = []

    def start(*args, tty=False):
        process = TerminalProcess(config, env, *args, tty=tty)
        processes.append(process)
        return process

    try:
        yield {
            "admin": admin,
            "store": store,
            "provider": provider,
            "project": project,
            "config": config,
            "env": env,
            "start": start,
            "workspace": workspace,
            "skill": skill,
            "origin": origin,
            "tmp": tmp_path,
        }
    finally:
        for process in processes:
            process.close()
        server.should_exit = True
        thread.join(timeout=40)
        admin.close()
        assert not thread.is_alive(), "Disposable Core did not shut down"


def test_paired_terminal_direct_chats_skills_goal_attachments_and_reconnect(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"]("shell", "--project", "Terminal Project")
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("/skills", expect="Skill for the next turn")
    terminal.send("review", expect="Selected $review")
    terminal.send("/attach project", expect="Core project files")
    terminal.send("notes.md", expect="Attach this exact excerpt")
    terminal.send("yes", expect="Excerpt attached")
    terminal.send("Summarize these synthetic notes", expect="Response complete.")
    sessions = admin.get(
        "chat-sessions", params={"engagement_id": "terminal-project"}
    ).json()
    session = next(
        row
        for row in sessions
        if row["backend"] == "provider" and not row["metadata"].get("control_assistant")
    )
    path = f"chat/sessions/{session['id']}"
    messages = admin.get(path + "/messages").json()
    assert len([row for row in messages if row["role"] == "user"]) == 1
    assert (
        messages[0]["metadata"].get("context_attachments")
        or messages[0]["metadata"].get("selected_context")
        or "notes" in messages[0]["content"]
    )
    document = data["tmp"] / "client-document.txt"
    document.write_text("An explicitly uploaded synthetic document.\n")
    terminal.send(
        '/attach local "' + str(document) + '"', expect="Upload client-document.txt"
    )
    terminal.send("yes", expect="Attach this exact excerpt")
    terminal.send("yes", expect="Excerpt attached")
    image = data["tmp"] / "client-image.png"
    Image.new("RGB", (2, 2), "white").save(image)
    terminal.send(
        '/attach image "' + str(image) + '"', expect="Attached client-image.png"
    )
    terminal.send(
        "Review the attached synthetic document and image", expect="Response complete."
    )
    assert (
        len(
            [
                row
                for row in admin.get(path + "/messages").json()
                if row["role"] == "user"
            ]
        )
        == 2
    )
    terminal.send("/settings", expect="Assistant settings")
    terminal.send("Plan before acting", expect="Saved in Core for the next turn")
    observed = admin.get(f"chat-sessions/{session['id']}/assistant-config").json()
    assert observed["settings"]["planning_enabled"] is True
    # A concurrent device changes the setting; the next client read must use it.
    changed = admin.patch(
        f"chat-sessions/{session['id']}/assistant-config",
        json={
            "expected_revision": observed["revision"],
            "settings": {"planning_enabled": False},
        },
    )
    assert changed.is_success, changed.text
    terminal.send("/goal create", expect="Goal objective")
    terminal.send("Retain a synthetic summary", expect="Completion criteria")
    terminal.send("The summary is saved", expect="Plan steps")
    terminal.send("Read the notes", expect="Resume instruction")
    terminal.send("", expect="Token budget")
    terminal.send("", expect="Active-time budget")
    terminal.send("", expect="Step budget")
    terminal.send("1", expect="Child-goal budget")
    terminal.send("0", expect="Goal saved in Core")
    terminal.send("/goal skills", expect="Goal skills")
    terminal.send("review", expect="Goal skills")
    terminal.send("Done", expect="Core goal status")
    goal = admin.get(path + "/goal").json()
    assert goal["skill_snapshots"][0]["name"] == "review"
    snapshot = goal["skill_snapshots"][0]["sha256"]
    data["skill"].write_text(
        "---\nname: review\ndescription: Changed source\n---\nChanged after snapshot.\n"
    )
    assert admin.get(path + "/goal").json()["skill_snapshots"][0]["sha256"] == snapshot
    terminal.close()
    before = admin.get(path + "/messages").json()
    reopened = data["start"]("shell")
    reopened.expect("/help for commands")
    reopened.send("/history", expect="Synthetic provider answer.")
    assert admin.get(path + "/messages").json() == before
    reopened.send("/chats", expect="Harness chat")
    reopened.send("Harness chat", expect="Ready for your next message")
    reopened.send("ask fixture question", expect="Action required")
    reopened.send("/input", expect="Synthetic terminal question")
    reopened.send("Synthetic terminal question", expect="Answer question")
    reopened.send("Answer", expect="Choose a fixture answer")
    reopened.send("continue", expect="Question status: answered")
    reopened.send("/follow", expect="Response complete.")
    assert not (data["workspace"] / "draft.txt").exists()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python source; no frontend",
                "fixture": "real TLS LAN Core, inert provider/harness and deterministic embedding",
                "checks": [
                    "paired direct chat",
                    "skill selection",
                    "Core file excerpt",
                    "settings and concurrent device",
                    "goal snapshot retention",
                    "reconnect without replay",
                    "harness input and follow",
                ],
                "result": "passed",
            }
        )
    )


def test_paired_full_screen_tui_shows_progress_and_saved_answer(lan_core):
    data = lan_core
    terminal = data["start"]("shell", "--project", "Terminal Project", tty=True)
    terminal.expect("Message or /command")
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("Answer a synthetic markdown layout question", expect="You · sending")
    terminal.expect("Synthetic provider answer.")
    terminal.expect_screen(
        "Synthetic provider answer.",
        "HEIF",
        "Response complete.",
        absent=("**Synthetic", "|---|", "/projects/terminal-project/workbench"),
    )
    os.write(terminal.input_fd, b"\x1b[21~")  # F10 restores copyable source.
    terminal.expect_screen("F10 Render", "**Synthetic provider answer.**")
    sessions = (
        data["admin"]
        .get("chat-sessions", params={"engagement_id": "terminal-project"})
        .json()
    )
    saved = next(
        row
        for row in sessions
        if row["backend"] == "provider" and not row["metadata"].get("control_assistant")
    )
    messages = data["admin"].get(f"chat/sessions/{saved['id']}/messages").json()
    assert [
        row["role"] for row in messages if row["role"] in {"user", "assistant"}
    ] == [
        "user",
        "assistant",
    ]
    assert messages[-1]["content"].startswith("**Synthetic provider answer.**")
    assert "| HEIF | Paused |" in messages[-1]["content"]
    terminal.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "build": "candidate Python TUI and real authenticated TLS LAN Core",
                "fixture": "inert provider, synthetic project and credential",
                "checks": [
                    "visible acceptance",
                    "rendered streamed Markdown",
                    "source toggle",
                    "saved readback",
                ],
                "result": "passed",
            }
        )
    )


def test_terminal_queue_detach_approval_and_ctrl_c_use_core_state(lan_core):
    data = lan_core
    admin = data["admin"]
    terminal = data["start"]("shell", "--project", "Terminal Project")
    terminal.expect("New conversation")
    terminal.send("New conversation", expect="/help for commands")
    terminal.send("hold response")
    assert data["provider"].holding.wait(15), "Inert provider did not reach its hold"
    sessions = admin.get(
        "chat-sessions", params={"engagement_id": "terminal-project"}
    ).json()
    provider = next(row for row in sessions if row["backend"] == "provider")
    path = f"chat/sessions/{provider['id']}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        saved = json.loads(data["config"].read_text())
        selection = saved["cores"][data["origin"]].get("selection", {})
        if selection.get("session_id") == provider["id"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("Terminal did not bind the accepted Core conversation")
    terminal.send("/status", expect="Working.")
    active = admin.get(path + "/state").json()
    assert active["busy"], active
    terminal.send("next after hold", expect="Saved in Core's follow-up queue")
    terminal.send("/detach", expect="Viewer detached")
    assert admin.get(path + "/state").json()["busy"]
    terminal.close()
    assert admin.get(path + "/state").json()["busy"]
    reopened = data["start"]("shell")
    reopened.expect("/help for commands")
    reopened.send("/queue show", expect="next after hold")
    reopened.send("/stop", expect="Turn cancelled.")
    from nebula.v3.domain import ChatTurn

    assert data["store"].get(ChatTurn, active["turn_id"]).status.value == "cancelled"
    reopened.send("/chats", expect="Harness chat")
    reopened.send("Harness chat", expect="Ready for your next message")
    reopened.send("request fixture approval", expect="Action required")
    reopened.send("/approvals", expect="Pending approval")
    reopened.send("Review the requested action", expect="Decision")
    reopened.send("Reject", expect="Decision recorded: rejected")
    reopened.expect("Response complete.")
    assert not (data["workspace"] / "draft.txt").exists()
    reopened.send(
        "/chats " + provider["id"],
        expect="You:",
    )
    reopened.close()
    terminal_tty = data["start"]("shell", tty=True)
    terminal_tty.expect("/help for commands")
    data["provider"].holding.clear()
    terminal_tty.send("hold response again")
    assert data["provider"].holding.wait(15), "Inert provider did not reach its hold"
    terminal_tty.send("/status", expect="Working.")
    active = admin.get(path + "/state").json()
    assert active["busy"]
    os.write(terminal_tty.input_fd, b"\x03")
    terminal_tty.expect("Turn cancelled.")
    assert data["store"].get(ChatTurn, active["turn_id"]).status.value == "cancelled"
    terminal_tty.close()
    print(
        json.dumps(
            {
                "origin": data["origin"],
                "fixture": "real TLS LAN Core with inert work",
                "checks": [
                    "durable queue",
                    "detach and quit preserve work",
                    "reconnect",
                    "exact turn stop",
                    "approval review",
                    "TTY Ctrl-C",
                ],
                "result": "passed",
            }
        )
    )
