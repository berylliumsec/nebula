"""Inert managed ACP for public HostAPI/IPC browser acceptance.

Copied as acceptance_standing_instructions_native.py into verified bundles.
Only transport/model output is synthetic. The production Grok adapter, native
system-head guard, Core storage, authentication and turn lifecycle remain real.
Module-level imports and evidence() use only the standard library.
"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

RULE_ALPHA = "NATIVE_STANDING_ALPHA: Inspect synthetic fixture data only."
RULE_BETA = "NATIVE_STANDING_BETA: Verify synthetic fixture results before replying."
PROFILE_NAME = "Inert native instruction fixture"
MODEL = "synthetic-grok"
CONFIG = "standing-native-fixture.json"
CONTROL_SUFFIX = "acceptance-standing-instructions"


def _load_config(root: Path):
    path = root / CONFIG
    return json.loads(path.read_text()) if path.is_file() else None


def _safe(value, root: Path):
    if isinstance(value, dict):
        result = {key: _safe(item, root) for key, item in value.items()}
        if (
            any(
                part in str(value.get("name", "")).lower()
                for part in ("token", "secret", "password")
            )
            and "value" in result
        ):
            result["value"] = "<redacted synthetic credential>"
        for key in result:
            if key.lower() in {"authorization", "token", "secret", "password"}:
                result[key] = "<redacted synthetic credential>"
        return result
    if isinstance(value, list):
        return [_safe(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(str(root), "<fixture-data>")
    return value


class NativeState:
    def __init__(self, root: Path):
        self.root = root
        self.config = _load_config(root)
        if not self.config:
            raise RuntimeError("native instruction fixture was not initialized")
        self.folder = root / self.config["relative_root"]
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / "native-state.json"

    @contextmanager
    def locked(self):
        with (self.folder / "state.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {
                    "protocol": "nebula.synthetic-native-instructions/v1",
                    "sessions": {},
                    "requests": [],
                    "events": [],
                    "compactions": [],
                    "last_managed_session": None,
                }
            )
            yield state
            temporary = self.path.with_name("native-state-" + uuid4().hex + ".tmp")
            temporary.write_text(json.dumps(state))
            temporary.chmod(0o600)
            os.replace(temporary, self.path)

    def head(self, native_id: str, *, account_home: Path):
        return (
            account_home
            / "sessions"
            / "synthetic-project"
            / native_id
            / "system_prompt.txt"
        )

    def persist_head(self, native_id: str, text: str, *, account_home: Path):
        path = self.head(native_id, account_home=account_home)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(text.encode("utf-8"))
        path.chmod(0o600)

    def compact(self):
        with self.locked() as state:
            identity = state["last_managed_session"]
            if identity is None:
                raise ValueError("send a native turn before compaction")
            session = state["sessions"][identity]
            before = hashlib.sha256(session["system"].encode()).hexdigest()
            removed = len(session["history"])
            session["history"] = [
                {
                    "role": "user",
                    "text": "SYNTHETIC LOSSY SUMMARY: Prior work occurred; standing instructions are omitted.",
                }
            ]
            after = hashlib.sha256(session["system"].encode()).hexdigest()
            state["compactions"].append(
                {
                    "native_session_id": identity,
                    "removed_items": removed,
                    "system_sha256_before": before,
                    "system_sha256_after": after,
                }
            )
            return {"native_session_id": identity, "system_preserved": before == after}


def evidence(root: Path):
    """Sanitized, stdlib-only readback for the outer acceptance controller."""
    config = _load_config(root)
    if config is None:
        return {"enabled": False}
    native = NativeState(root)
    with native.locked() as state:
        result = deepcopy(state)
    identity = result["last_managed_session"]
    active = result["sessions"].get(identity) if identity else None
    try:
        head = (
            native.head(identity, account_home=root / active["account_home"])
            .read_bytes()
            .decode("utf-8")
            if active
            else ""
        )
        head_available = bool(identity)
    except FileNotFoundError:
        head, head_available = "", False
    effective = (
        head + "\n" + "\n".join(item["text"] for item in active["history"])
        if active
        else ""
    )
    return _safe(
        {
            "enabled": True,
            "project_id": config["project_id"],
            "session_id": config["session_id"],
            "active_native_session_id": identity,
            "system_head": head,
            "system_head_available": head_available,
            "system_head_sha256": hashlib.sha256(head.encode()).hexdigest(),
            "effective_context": effective,
            "alpha_copies": effective.count(RULE_ALPHA),
            "beta_copies": effective.count(RULE_BETA),
            "native": result,
        },
        root,
    )


async def initialize(store, *, root: Path, project_id: str, session_id: str):
    from nebula.v3.domain import (
        ChatBackend,
        ChatDecision,
        ChatSession,
        HarnessCapabilities,
        HarnessKind,
        HarnessProfile,
        HarnessSession,
        utc_now,
    )

    root = root.resolve()
    relative = "standing-native-" + hashlib.sha256(project_id.encode()).hexdigest()[:16]
    folder = root / relative
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = folder / "inert-grok"
    marker.write_text(
        '#!/bin/sh\nprintf "Inert acceptance transport only\\n" >&2\nexit 64\n'
    )
    marker.chmod(0o700)
    profile = await store.create(
        HarnessProfile(
            id="standing-native-profile",
            name=PROFILE_NAME,
            kind=HarnessKind.GROK_ACP,
            executable=str(marker),
            home_directory=str(folder / "account"),
            default_model=MODEL,
            privacy={"local_only": True, "permits_sensitive_data": True},
            capabilities=HarnessCapabilities(
                models=[MODEL],
                interruption=True,
                mcp=True,
                adapter_version="synthetic-acp/v1",
                checked_at=utc_now(),
            ),
        )
    )
    session = await store.create(
        HarnessSession(
            id="standing-native-initial",
            engagement_id=project_id,
            harness_profile_id=profile.id,
            model=MODEL,
            status="idle",
        )
    )
    chat = await store.get(ChatSession, session_id)
    await store.update(
        ChatSession,
        chat.id,
        {
            "backend": ChatBackend.HARNESS,
            "provider_profile_id": None,
            "harness_profile_id": profile.id,
            "harness_session_id": session.id,
            "model": MODEL,
            "metadata": {
                **chat.metadata,
                "initial_title_state": "operator",
                "context_management": "runtime_managed",
                "core_tool_selection": {
                    "allowed_tool_names": [],
                    "allow_mcp_catalog": False,
                    "ssh_environment_ids": [],
                    "skill_selections": [],
                },
            },
        },
        expected_revision=chat.revision,
    )
    decision = await store.create(
        ChatDecision(
            id="standing-native-constraint",
            engagement_id=project_id,
            session_id=session_id,
            kind="constraint",
            text=RULE_ALPHA,
        )
    )
    config = {
        "project_id": project_id,
        "session_id": session_id,
        "relative_root": relative,
        "profile_id": profile.id,
        "decision_id": decision.id,
    }
    (root / CONFIG).write_text(json.dumps(config))
    (root / CONFIG).chmod(0o600)
    return {
        **config,
        "profile_name": PROFILE_NAME,
        "model": MODEL,
        "rule_alpha": RULE_ALPHA,
        "rule_beta": RULE_BETA,
        "control_path": f"/api/v1/engagements/{project_id}/{CONTROL_SUFFIX}",
        "auxiliary_assets": [],
        "fixture_protocol": "nebula.synthetic-native-instructions/v1",
    }


class InertRpc:
    connection_state = "connected"

    def __init__(
        self,
        native: NativeState,
        *,
        account_home: Path,
        core_session_id: str,
        managed: bool,
        version: str,
    ):
        self.native, self.core_session_id, self.managed, self.version = (
            native,
            core_session_id,
            managed,
            version,
        )
        self.events = asyncio.Queue()
        self.account_home = account_home
        self.relative_account_home = str(account_home.relative_to(native.root))

    async def request(self, method, params):
        import sys

        emitted = None
        with self.native.locked() as state:
            if len(state["requests"]) >= 256:
                raise RuntimeError("native fixture request budget exhausted")
            row = {
                "method": method,
                "params": _safe(deepcopy(params), self.native.root),
                "core_session_id": self.core_session_id,
                "managed": self.managed,
                "release_version": self.version,
                "pid": os.getpid(),
                "fixture_source": __file__,
                "python": sys.executable,
            }
            state["requests"].append(row)
            if method == "initialize":
                result = {
                    "protocolVersion": 1,
                    "agentInfo": {
                        "name": "inert-native-fixture",
                        "version": "synthetic-native-v1",
                    },
                    "agentCapabilities": {"loadSession": True},
                }
            elif method == "session/new":
                identity = str(uuid4())
                rules = params.get("_meta", {}).get("rules", "")
                head = (
                    "Synthetic native system head.\n<human_rules>\n"
                    + rules
                    + "\n</human_rules>"
                )
                state["sessions"][identity] = {
                    "system": head,
                    "history": [],
                    "core_session_id": self.core_session_id,
                    "managed": self.managed,
                    "account_home": self.relative_account_home,
                }
                if self.managed:
                    state["last_managed_session"] = identity
                self.native.persist_head(identity, head, account_home=self.account_home)
                row["native_session_id"] = identity
                result = {"sessionId": identity}
            elif method == "session/load":
                identity = params["sessionId"]
                session = state["sessions"][identity]
                if session["core_session_id"] != self.core_session_id:
                    raise ValueError("native session belongs to another Core context")
                if session["account_home"] != self.relative_account_home:
                    raise ValueError("native session belongs to another account home")
                self.native.persist_head(
                    identity, session["system"], account_home=self.account_home
                )
                row["native_session_id"] = identity
                result = {}
            elif method == "session/prompt":
                identity = params["sessionId"]
                row["native_session_id"] = identity
                session = state["sessions"][identity]
                session["history"].append(
                    {
                        "role": "user",
                        "text": "\n".join(item["text"] for item in params["prompt"]),
                    }
                )
                effective = (
                    session["system"]
                    + "\n"
                    + "\n".join(item["text"] for item in session["history"])
                )
                alpha, beta = effective.count(RULE_ALPHA), effective.count(RULE_BETA)
                valid = (alpha, beta) in {(1, 0), (0, 1)}
                label = "ALPHA" if alpha else "BETA"
                answer = (
                    f"Native fixture confirmed {label}."
                    if valid
                    else "Native fixture has missing or duplicate standing instructions."
                )
                if not self.managed:
                    answer = "Synthetic native analysis result."
                session["history"].append({"role": "assistant", "text": answer})
                emitted = {
                    "method": "session/update",
                    "params": {
                        "sessionId": identity,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": answer},
                        },
                    },
                }
                state["events"].append(
                    {
                        "native_session_id": identity,
                        "frame": emitted,
                        "alpha_copies": alpha,
                        "beta_copies": beta,
                        "managed": self.managed,
                    }
                )
                if self.managed:
                    state["last_managed_session"] = identity
                result = {
                    "stopReason": "end_turn" if valid or not self.managed else "refusal"
                }
            else:
                result = {}
        if emitted is not None:
            await self.events.put(emitted)
        return result

    async def notify(self, method, params=None):
        return None

    async def respond(self, request_id, result):
        raise AssertionError("inert native fixture advertises no permission requests")

    async def respond_error(self, request_id, code, message):
        raise AssertionError("inert native fixture emitted no client requests")

    async def close(self):
        self.connection_state = "disconnected"


def configure_app(app, *, root: Path, lease, version: str):
    """Install only this retained module's inert native transport before lifespan."""
    from fastapi import HTTPException, Request
    from nebula.v3.domain import ChatSession, HarnessKind, HarnessSession
    from nebula.v3.harnesses import (
        GrokAcpAdapter,
        HarnessHealth,
        _gateway_only_core_planned_session,
        _harness_home,
    )

    root = root.resolve()
    config = _load_config(root)
    if config is None or config["project_id"] != lease.project_id:
        return
    native = NativeState(root)
    runtime = app.state.harness_runtime_service
    original_factory = runtime.adapter_factory

    class Adapter(GrokAcpAdapter):
        async def _connect(self, profile, workspace, session=None):
            if session is None:
                raise RuntimeError(
                    "fixture native connections need a scoped Core session"
                )
            isolated = session.metadata.get("control_assistant") is True or (
                _gateway_only_core_planned_session(session, profile.kind)
            )
            account_home = (
                workspace.parent / f"{workspace.name}.settings-home"
                if isolated
                else _harness_home(profile)
            ).resolve()
            return InertRpc(
                native,
                account_home=account_home,
                core_session_id=session.id,
                managed="standing_instructions" in session.metadata,
                version=version,
            )

        async def open_memory_managed(self, request, memory):
            # This in-process transport creates no native worker or resource job.
            # Actual Grok open/channel-readback and connection.run_turn still run.
            return await self.open(request)

        async def probe(self, profile, credential_store):
            return HarnessHealth(
                profile_id=profile.id,
                kind=profile.kind,
                healthy=True,
                harness_version="synthetic-native-fixture",
                capabilities=profile.capabilities,
                authentication_state="verified",
                session_state="unverified",
                turn_state="unverified",
            )

    adapter = Adapter()
    runtime.adapter_factory = lambda kind: (
        adapter if kind == HarnessKind.GROK_ACP else original_factory(kind)
    )
    runtime.host_load = None  # No native process/model work exists in this fixture.

    async def control(request: Request):
        body = await request.json()
        operation = body.get("operation", "capture")
        chat = await runtime.store.get(ChatSession, config["session_id"])
        if chat.engagement_id != lease.project_id or not chat.harness_session_id:
            raise HTTPException(409, "native fixture chat binding changed")
        session = await runtime.store.get(HarnessSession, chat.harness_session_id)
        if operation != "capture" and session.id in runtime._active:
            raise HTTPException(
                409, "wait for the native turn to finish before fixture control"
            )
        if operation == "compact":
            native.compact()
        elif operation == "reconnect":
            connection = runtime._connections.get(session.id)
            if connection is not None:
                await runtime._discard_connection(
                    session.id, connection, reason="synthetic_native_reconnect"
                )
        elif operation != "capture":
            raise HTTPException(400, "unknown native fixture operation")
        result = evidence(root)
        result["core_harness_session_id"] = session.id
        result["turn_active"] = session.id in runtime._active
        result["channel_receipt"] = session.metadata.get("instruction_channel")
        result["standing_snapshot"] = session.metadata.get("standing_instructions")
        return result

    # Reuse the actual production completion route's authentication dependencies.
    dependencies = next(
        route.dependencies
        for route in app.routes
        if getattr(route, "path", None) == "/api/v1/chat/completions"
    )
    app.add_api_route(
        f"/api/v1/engagements/{lease.project_id}/{CONTROL_SUFFIX}",
        control,
        methods=["POST"],
        dependencies=dependencies,
        response_model=None,
        name="synthetic_standing_instructions_control",
    )
