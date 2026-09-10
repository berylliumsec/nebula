"""Read-only authoritative display-state contracts."""

import pytest

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
