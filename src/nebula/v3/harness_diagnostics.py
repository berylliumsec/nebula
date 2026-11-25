"""The canonical bounded diagnostic turn without a project runtime service."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
from typing import Any, Callable, Literal, TYPE_CHECKING
from uuid import uuid4

from .credentials import CredentialStore
from .diagnostic_guidance import reason_code_for
from .diagnostics import record_caught_exception
from .domain import (
    HarnessAuthMode,
    HarnessKind,
    HarnessNativeCapabilities,
    HarnessProfile,
    HarnessSession,
    utc_now,
)
from .storage import NebulaStore

if TYPE_CHECKING:
    from .harnesses import HarnessHealth
    from .memory_runtime import MemoryComputeController


async def run_harness_test_turn(
    store: NebulaStore,
    profile_id: str,
    *,
    credentials: CredentialStore,
    adapter_factory: Callable[..., Any],
    open_connection: Callable[..., Any],
) -> HarnessHealth:
    """Spend one bounded provider turn outside every durable project session."""
    from .harnesses import (
        AdapterOpenRequest,
        HarnessConfigurationError,
        HarnessConnection,
        HarnessHealth,
        HarnessPermissionRequest,
        HarnessTransportError,
        PermissionTicket,
    )

    profile = await store.get(HarnessProfile, profile_id)
    if not profile.enabled:
        raise HarnessConfigurationError("Enable this harness before testing a turn")
    model = profile.default_model or next(iter(profile.capabilities.models), None)
    if not model:
        raise HarnessConfigurationError("Check this harness to discover a model first")
    state: Literal["verified", "failed", "unverified"] = "failed"
    session_state: Literal["verified", "failed", "unverified"] = "failed"
    reason: str | None = None
    connection: HarnessConnection | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="nebula-harness-check-"
        ) as workspace_name:
            workspace = Path(workspace_name)
            session = HarnessSession(
                id=str(uuid4()),
                engagement_id="diagnostic",
                harness_profile_id=profile.id,
                model=model,
                metadata={
                    "native_capabilities": HarnessNativeCapabilities().model_dump()
                },
            )

            async def deny_permission(
                _request: HarnessPermissionRequest,
            ) -> PermissionTicket:
                raise HarnessConfigurationError("Diagnostic turns cannot use tools")

            request = AdapterOpenRequest(
                profile=profile.model_copy(
                    update={"native_capabilities": HarnessNativeCapabilities()}
                ),
                session=session,
                workspace=workspace,
                mcp_profiles=(),
                credential_store=credentials,
                permission_handler=deny_permission,
            )
            adapter = adapter_factory(profile.kind)
            connection = await open_connection(adapter, request)
            session_state = "verified"
            completed = False
            reply = ""
            reply_bytes = 0
            async with asyncio.timeout(90):
                async for event in connection.run_turn(
                    "Reply with exactly OK. Do not use tools.", model=model
                ):
                    if event.type == "error":
                        raise HarnessTransportError(
                            event.message or "Diagnostic turn failed"
                        )
                    if (
                        event.type in {"tool_started", "tool_completed"}
                        or event.item_kind == "tool"
                    ):
                        raise HarnessConfigurationError(
                            "Diagnostic turn attempted a tool"
                        )
                    completed = completed or event.type == "completed"
                    if event.type == "completed" and event.message:
                        reply_bytes = len(event.message.encode())
                        if reply_bytes > 4096:
                            raise HarnessTransportError(
                                "Diagnostic response exceeded its bounded test size"
                            )
                        reply = event.message
                    elif event.type == "message_delta" and event.delta:
                        reply_bytes += len(event.delta.encode())
                        if reply_bytes > 4096:
                            raise HarnessTransportError(
                                "Diagnostic response exceeded its bounded test size"
                            )
                        reply += event.delta
            if not completed or not reply.strip():
                raise HarnessTransportError(
                    "Diagnostic turn ended without a text answer"
                )
            state = "verified"
            if connection.external_session_id:
                resumed = session.model_copy(
                    update={"external_session_id": connection.external_session_id}
                )
                await connection.close()
                connection = None
                resume_request = AdapterOpenRequest(
                    profile=request.profile,
                    session=resumed,
                    workspace=workspace,
                    mcp_profiles=(),
                    credential_store=credentials,
                    permission_handler=deny_permission,
                )
                connection = await open_connection(adapter, resume_request)
                if connection.external_session_id != resumed.external_session_id:
                    raise HarnessTransportError(
                        "Diagnostic session resumed a different id"
                    )
            await connection.close()
            connection = None
    except Exception as exc:
        # A vendor turn failure carries its own classification, as chat turns use.
        reason = reason_code_for(
            exc, feature="harnesses", supplied=getattr(exc, "reason_code", None)
        )
        record_caught_exception(
            "harnesses",
            "harnesses.test_turn_failed",
            "A diagnostic harness turn failed.",
            exc,
            stage="test-turn",
        )
        if state == "verified":
            session_state = "failed"
        elif session_state != "verified":
            session_state = "failed"
    finally:
        if connection is not None:
            with suppress(Exception):
                await connection.close()
    detail = reason
    signed_out = reason == "authentication_failed"
    if (
        signed_out
        and profile.kind == HarnessKind.CLAUDE_AGENT_SDK
        and profile.auth_mode == HarnessAuthMode.EXISTING_SESSION
    ):
        from .harnesses import _claude_not_logged_in_detail

        # Say where the CLI looked for its login and how to sign in there.
        detail = _claude_not_logged_in_detail(profile)
    current = await store.get(HarnessProfile, profile_id)
    capabilities = current.capabilities.model_copy(
        update={
            **({"detail": detail} if signed_out and detail != reason else {}),
            "authentication_state": "failed"
            if signed_out
            else current.capabilities.authentication_state,
            "session_state": session_state,
            "turn_state": state,
            "last_successful_turn_at": utc_now()
            if state == "verified"
            else current.capabilities.last_successful_turn_at,
            "last_turn_failure_reason": reason,
            "exercised_capabilities": list(
                dict.fromkeys(
                    [
                        *current.capabilities.exercised_capabilities,
                        *(
                            ("session_create", "model_turn", "session_resume")
                            if state == "verified" and session_state == "verified"
                            else ()
                        ),
                    ]
                )
            ),
        }
    )
    (
        await store.update(
            HarnessProfile,
            profile_id,
            {"capabilities": capabilities},
            expected_revision=current.revision,
        )
    )
    return HarnessHealth(
        profile_id=profile_id,
        healthy=state == "verified" and session_state == "verified",
        kind=profile.kind,
        capabilities=capabilities,
        detail=detail,
        authentication_state=capabilities.authentication_state,
        session_state=session_state,
        turn_state=state,
        last_successful_turn_at=capabilities.last_successful_turn_at,
    )


async def run_host_harness_test_turn(settings: Any, profile_id: str) -> HarnessHealth:
    """Run a bounded diagnostic with the adapter's available open contract."""
    from .harnesses import HarnessRuntimeService

    controller: MemoryComputeController | None = getattr(
        settings.observations, "memory_compute", None
    )

    class BoundedConnection:
        def __init__(self, connection: Any):
            self.connection = connection

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        async def close(self) -> None:
            await asyncio.wait_for(self.connection.close(), timeout=5)

    async def open_connection(adapter: Any, request: Any) -> Any:
        opener = getattr(adapter, "open_memory_managed", None)
        opening = (
            opener(request, controller)
            if callable(opener) and controller is not None
            else adapter.open(request)
        )
        connection = await asyncio.wait_for(opening, timeout=15)
        return BoundedConnection(connection)

    # Includes bounded open, canonical 90-second fixed turn, resume and cleanup;
    # it completes before the accepted host effect's own 150-second boundary.
    async with asyncio.timeout(140):
        return await run_harness_test_turn(
            settings.store,
            profile_id,
            credentials=settings.credentials,
            adapter_factory=settings.harness_factory
            or HarnessRuntimeService._default_adapter,
            open_connection=open_connection,
        )


async def run_harness_one_shot(
    profile: HarnessProfile,
    model: str,
    prompt: str,
    *,
    credentials: CredentialStore,
    adapter_factory: Callable[..., Any],
    open_connection: Callable[..., Any],
    timeout_seconds: float = 600,
    max_reply_bytes: int = 64_000,
) -> str:
    """One tool-free turn in a fresh harness session; returns the reply text.

    Used by Butler's independent closure reviewer. The session is new (no
    vendor session id, so nothing is resumed), runs in an empty private
    scratch directory, has every vendor-native capability off, no MCP server
    and no Core gateway, and every permission request is denied. Any tool
    event ends the turn with an error, so a reply is never the product of a
    tool call.
    """
    from .harnesses import (
        AdapterOpenRequest,
        HarnessConfigurationError,
        HarnessPermissionRequest,
        HarnessTransportError,
        PermissionTicket,
    )

    if not profile.enabled:
        raise HarnessConfigurationError(f"harness {profile.name!r} is disabled")
    with tempfile.TemporaryDirectory(prefix="nebula-harness-review-") as name:
        workspace = Path(name)
        no_tools = HarnessNativeCapabilities()
        session = HarnessSession(
            id=str(uuid4()),
            engagement_id="independent-review",
            harness_profile_id=profile.id,
            model=model,
            metadata={"native_capabilities": no_tools.model_dump()},
        )

        async def deny_permission(
            _request: HarnessPermissionRequest,
        ) -> PermissionTicket:
            raise HarnessConfigurationError("The independent reviewer cannot use tools")

        request = AdapterOpenRequest(
            profile=profile.model_copy(update={"native_capabilities": no_tools}),
            session=session,
            workspace=workspace,
            mcp_profiles=(),
            credential_store=credentials,
            permission_handler=deny_permission,
        )
        connection = await open_connection(adapter_factory(profile.kind), request)
        try:
            reply = ""
            final = ""
            size = 0
            async with asyncio.timeout(timeout_seconds):
                async for event in connection.run_turn(prompt, model=model):
                    if event.type == "error":
                        raise HarnessTransportError(
                            event.message or "Reviewer turn failed"
                        )
                    if (
                        event.type in {"tool_started", "tool_completed"}
                        or event.item_kind == "tool"
                    ):
                        raise HarnessConfigurationError(
                            "The independent reviewer attempted a tool"
                        )
                    if event.type == "message_delta" and event.delta:
                        size += len(event.delta.encode())
                        reply += event.delta
                    elif event.type == "completed" and event.message:
                        final = event.message
                        size = max(size, len(final.encode()))
                    if size > max_reply_bytes:
                        raise HarnessTransportError(
                            "Reviewer response exceeded its bounded size"
                        )
            text = (final or reply).strip()
            if not text:
                raise HarnessTransportError("Reviewer turn ended without a text answer")
            return text
        finally:
            with suppress(Exception):
                await connection.close()
