"""#1100: a peer message saves the conversation while a harness send is prepared.

``AgentMessageService.send`` is the real concurrent writer. It lands between
``prepare_chat``'s read of the conversation and its guarded write, both
before the turn is committed (rebind, planning, edit handoff) and after it
(the tool selection saved beside a committed turn).
"""

from __future__ import annotations

import asyncio

import pytest

from nebula.v3.chat_agent_messages import AgentMessageService
from nebula.v3.domain import (
    ChatMessage,
    ChatSession,
    ChatTurn,
    HarnessSession,
    HarnessTurn,
)
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)
from tests.v3.test_chat_agent_messages import _invocation
from tests.v3.test_harness_provider_subagents import _setup

pytestmark = pytest.mark.anyio


async def _prepare(runtime, project, harness, prompt, chat_id=None, **extra):
    return await runtime.prepare_chat(
        engagement_id=project.id,
        profile_id=harness.id,
        model=extra.pop("model", None),
        prompt=prompt,
        chat_session_id=chat_id,
        harness_session_id=None,
        mcp_server_ids=[],
        allow_agent_messaging=True,
        **extra,
    )


def _gate(owner, name, *, when=lambda *a, **k: True, after=False):
    """Pause ``owner.name`` once, before or after the real call."""

    original = getattr(owner, name)
    gate = {"armed": True, "paused": asyncio.Event(), "resume": asyncio.Event()}

    async def wrapper(*args, **kwargs):
        hit = gate["armed"] and when(*args, **kwargs)
        if hit:
            gate["armed"] = False
        if hit and not after:
            gate["paused"].set()
            await gate["resume"].wait()
        result = await original(*args, **kwargs)
        if hit and after:
            gate["result"] = result
            gate["paused"].set()
            await gate["resume"].wait()
        return result

    setattr(owner, name, wrapper)
    return gate


async def _first_turn(tmp_path):
    store, project, harness, chat, adapter, runtime = await _setup(tmp_path)
    parent, _, first = await _prepare(runtime, project, harness, "First.")
    await (await runtime.start_chat_turn(first.id))
    parent = await store.get(ChatSession, parent.id)
    sender = await store.create(
        ChatSession(
            engagement_id=project.id,
            title="Peer coordinator",
            provider_profile_id="provider",
            model="model-a",
            metadata={"allow_agent_messaging": True},
        )
    )
    return store, project, harness, chat, runtime, parent, sender


async def _peer_write(store, sender, tmp_path, parent_id, key):
    before = (await store.get(ChatSession, parent_id)).revision
    await AgentMessageService(store).send(
        _invocation(sender, tmp_path, key=key), parent_id, f"Peer status {key}."
    )
    assert (await store.get(ChatSession, parent_id)).revision > before


async def _send_with_peer_write(store, sender, tmp_path, parent, gate, send, key):
    task = asyncio.create_task(send)
    await asyncio.wait_for(gate["paused"].wait(), 30)
    await _peer_write(store, sender, tmp_path, parent.id, key)
    gate["resume"].set()
    return await task


async def _user_messages(store, chat_id):
    return [
        message.content
        for message in await store.list_entities(ChatMessage, limit=1_000)
        if message.session_id == chat_id and message.role.value == "user"
    ]


async def test_saved_turn_keeps_its_conversation_after_a_peer_message(tmp_path):
    store, project, harness, chat, runtime, parent, sender = await _first_turn(tmp_path)
    original = store.update
    gate = {"armed": True, "paused": asyncio.Event(), "resume": asyncio.Event()}

    async def update(model, entity_id, changes, *, expected_revision=None):
        metadata = changes.get("metadata") if isinstance(changes, dict) else None
        if (
            gate["armed"]
            and model is ChatSession
            and isinstance(metadata, dict)
            and "core_tool_selection" in metadata
        ):
            # The turn and the operator's message are committed by now.
            gate["armed"] = False
            gate["paused"].set()
            await gate["resume"].wait()
        return await original(
            model, entity_id, changes, expected_revision=expected_revision
        )

    store.update = update
    saved, chat_turn, harness_turn = await _send_with_peer_write(
        store,
        sender,
        tmp_path,
        parent,
        gate,
        _prepare(runtime, project, harness, "Second.", chat_id=parent.id),
        "post-commit",
    )
    try:
        assert saved.metadata["core_tool_selection"]["allow_mcp_catalog"] is not None
        assert (await store.get(ChatTurn, chat_turn.id)).session_id == parent.id
        assert (await store.get(HarnessTurn, harness_turn.id)).id == harness_turn.id
        # The turn starts and settles normally, so the next send is accepted.
        await (await runtime.start_chat_turn(harness_turn.id))
        await _prepare(runtime, project, harness, "Third.", chat_id=parent.id)
        assert "Second." in await _user_messages(store, parent.id)
    finally:
        await chat.shutdown()


async def test_settings_rollover_rebinds_after_a_peer_message(tmp_path):
    store, project, harness, chat, runtime, parent, sender = await _first_turn(tmp_path)
    gate = _gate(runtime, "create_session", after=True)
    saved, chat_turn, _ = await _send_with_peer_write(
        store,
        sender,
        tmp_path,
        parent,
        gate,
        _prepare(
            runtime,
            project,
            harness,
            "Second, new model.",
            parent.id,
            model="gpt-other",
        ),
        "rollover",
    )
    try:
        assert saved.harness_session_id == gate["result"].id
        assert saved.model == "gpt-other"
        assert saved.metadata["harness_session_rollovers"][-1]["to_session_id"] == (
            gate["result"].id
        )
        assert (await store.get(HarnessSession, gate["result"].id)).id
        assert chat_turn.session_id == parent.id
        assert "Second, new model." in await _user_messages(store, parent.id)
    finally:
        await chat.shutdown()


async def test_planning_toggle_survives_a_peer_message(tmp_path):
    store, project, harness, chat, runtime, parent, sender = await _first_turn(tmp_path)
    assert not parent.metadata.get("planning_enabled")
    gate = _gate(runtime, "_validate_harness_privacy")
    saved, _, _ = await _send_with_peer_write(
        store,
        sender,
        tmp_path,
        parent,
        gate,
        _prepare(
            runtime, project, harness, "Plan it.", parent.id, planning_enabled=True
        ),
        "planning",
    )
    try:
        assert saved.metadata["planning_enabled"] is True
        assert "Plan it." in await _user_messages(store, parent.id)
    finally:
        await chat.shutdown()


async def test_edit_handoff_is_applied_once_after_a_peer_message(tmp_path):
    store, project, harness, chat, runtime, parent, sender = await _first_turn(tmp_path)
    parent = await runtime.rewind_chat_session(parent)
    assert parent.metadata.get("harness_context_handoff_pending") is True
    gate = _gate(
        runtime,
        "_chat_handoff_context",
        when=lambda chat, **kw: (
            chat.metadata.get("harness_context_handoff_pending") is True
        ),
    )
    saved, _, _ = await _send_with_peer_write(
        store,
        sender,
        tmp_path,
        parent,
        gate,
        _prepare(runtime, project, harness, "Edited question.", parent.id),
        "handoff",
    )
    try:
        assert saved.metadata["harness_context_handoff_pending"] is False
        assert saved.metadata["harness_context_handoff_applied"] is True
        assert "Edited question." in await _user_messages(store, parent.id)
    finally:
        await chat.shutdown()
