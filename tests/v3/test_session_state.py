"""Read-only authoritative display-state contracts."""

import pytest
from datetime import timedelta

from nebula.v3.domain import Approval, ChatSession, ChatTurn, HarnessTurn
from tests.v3.test_approval_continuation import fixture


@pytest.mark.parametrize(
    "decision,execution",
    [
        ("pending", "waiting_approval"),
        ("approved", "continuing"),
        ("rejected", "continuing"),
        ("cancelled", "continuing"),
    ],
)
def test_snapshot_distinguishes_decision_from_progress(tmp_path, decision, execution):
    from nebula.v3.session_state import session_state

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    store.update(
        Approval, approval.id, {"status": decision}, expected_revision=approval.revision
    )
    chat = store.get(ChatSession, owner.session_id)
    before = store.get(ChatTurn, owner.id).revision
    state = session_state(store, chat, runtime)
    assert state["execution"] == execution
    assert len(state["pending"]) == (1 if decision == "pending" else 0)
    assert store.get(ChatTurn, owner.id).revision == before
    assert session_state(store, chat, runtime)["revision"] == state["revision"]


def test_snapshot_revision_advances_after_decision_and_keeps_other_requests(tmp_path):
    from nebula.v3.session_state import session_state

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    store.create(approval.model_copy(update={"id": "second-request"}))
    chat = store.get(ChatSession, owner.session_id)
    before = session_state(store, chat, runtime)
    store.update(
        Approval,
        approval.id,
        {"status": "approved"},
        expected_revision=approval.revision,
    )
    after = session_state(store, chat, runtime)
    assert after["revision"] > before["revision"]
    assert [item["id"] for item in after["pending"]] == ["second-request"]
    assert after["execution"] == "waiting_approval"


def test_terminal_harness_cannot_advertise_an_action_from_a_late_owner_record(tmp_path):
    from nebula.v3.session_state import session_state

    _, store, runtime, _, turn, owner = fixture(tmp_path)
    store.update(
        HarnessTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
    )
    state = session_state(store, store.get(ChatSession, owner.session_id), runtime)
    assert state["execution"] == "complete"
    assert state["pending"] == []
    assert "review" not in state["actions"]
    assert not state["busy"]


def test_revision_advances_even_when_the_clock_moves_backwards(tmp_path, monkeypatch):
    from nebula.v3.session_state import session_state
    from nebula.v3 import storage

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    # Move forward first, then backwards without violating the entity's
    # created_at invariant. This isolates projection ordering from validation.
    monkeypatch.setattr(
        storage, "utc_now", lambda: approval.updated_at + timedelta(days=2)
    )
    saved = store.update(
        Approval,
        approval.id,
        {"policy_rationale": "Later observation"},
        expected_revision=approval.revision,
    )
    before = session_state(store, chat, runtime)
    monkeypatch.setattr(
        storage, "utc_now", lambda: approval.updated_at + timedelta(days=1)
    )
    store.update(
        Approval, approval.id, {"status": "approved"}, expected_revision=saved.revision
    )
    after = session_state(store, chat, runtime)
    assert after["pending"] == []
    assert after["revision"] > before["revision"]


def test_revision_advances_when_a_pending_request_is_removed(tmp_path):
    from nebula.v3.session_state import session_state

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    before = session_state(store, chat, runtime)
    store.delete(Approval, approval.id)
    after = session_state(store, chat, runtime)
    assert after["pending"] == []
    assert after["revision"] > before["revision"]


def test_expiry_changes_revision_without_rewriting_the_decision(tmp_path, monkeypatch):
    from nebula.v3.session_state import session_state
    import importlib

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    expires = approval.updated_at + timedelta(minutes=1)
    saved = store.update(
        Approval,
        approval.id,
        {"expires_at": expires},
        expected_revision=approval.revision,
    )
    chat = store.get(ChatSession, owner.session_id)
    before = session_state(store, chat, runtime)
    monkeypatch.setattr(
        importlib.import_module("nebula.v3.session_state"),
        "utc_now",
        lambda: expires + timedelta(seconds=1),
    )
    after = session_state(store, chat, runtime)
    assert after["pending"] == []
    assert after["revision"] > before["revision"]
    assert store.get(Approval, approval.id).revision == saved.revision


def test_projection_revision_survives_reopen_and_concurrent_reads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from nebula.v3.session_state import session_state
    from nebula.v3.storage import NebulaStore

    _, store, runtime, approval, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    before = session_state(store, chat, runtime)
    store.update(
        Approval,
        approval.id,
        {"status": "approved"},
        expected_revision=approval.revision,
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        snapshots = list(
            pool.map(lambda _: session_state(store, chat, runtime), range(12))
        )
    assert {item["revision"] for item in snapshots} == {before["revision"] + 1}
    reopened = NebulaStore(str(store.database.engine.url))
    try:
        assert session_state(reopened, chat, runtime) == snapshots[0]
    finally:
        reopened.database.engine.dispose()


def test_unchanged_projection_does_not_acquire_write_lock(tmp_path, monkeypatch):
    from nebula.v3.session_state import session_state

    _, store, runtime, _, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    before = session_state(store, chat, runtime)

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("an unchanged session-state read acquired a write lock")

    monkeypatch.setattr(store, "_begin_run_write", unexpected_write)
    assert session_state(store, chat, runtime) == before


def test_projection_reloads_session_identity_and_removes_cache_on_deletion(tmp_path):
    from sqlalchemy import select
    from nebula.v3.database import SessionProjectionRow
    from nebula.v3.session_state import session_state
    from nebula.v3.storage import NotFoundError

    _, store, runtime, _, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    session_state(store, chat, runtime)
    store.delete(ChatSession, chat.id)
    with pytest.raises(NotFoundError):
        session_state(store, chat, runtime)
    with store.database.session() as database:
        assert database.scalar(select(SessionProjectionRow)) is None


def test_idle_transport_and_transport_exit_are_not_inferred_from_active_turn(tmp_path):
    from types import SimpleNamespace
    from nebula.v3.session_state import session_state

    _, store, runtime, _, turn, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    store.update(
        HarnessTurn, turn.id, {"status": "complete"}, expected_revision=turn.revision
    )
    connection = SimpleNamespace(connection_state="connected")
    runtime._connections[chat.harness_session_id] = connection
    alive = session_state(store, chat, runtime)
    assert alive["execution"] == "complete"
    assert alive["connection"] == "connected"
    connection.connection_state = "disconnected"
    closed = session_state(store, chat, runtime)
    assert closed["execution"] == "complete"
    assert closed["connection"] == "disconnected"
    assert closed["revision"] == alive["revision"] + 1


def test_catchup_pending_does_not_overwrite_connection_revision(tmp_path):
    from types import SimpleNamespace
    from nebula.v3.session_state import session_projection, session_state

    _, store, runtime, _, _, owner = fixture(tmp_path)
    chat = store.get(ChatSession, owner.session_id)
    runtime._connections[chat.harness_session_id] = SimpleNamespace(
        connection_state="connected"
    )
    before = session_state(store, chat, runtime)
    assert session_projection(store, chat)["pending"] == before["pending"]
    assert session_state(store, chat, runtime) == before
