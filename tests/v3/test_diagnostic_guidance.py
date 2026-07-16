from __future__ import annotations

from nebula.v3.diagnostic_guidance import (
    REASON_CODES,
    load_catalog,
    reason_code_for,
    resolve_incidents,
)
from nebula.v3.diagnostics import FEATURE_FILES
from nebula.v3.harnesses import HarnessTransportError


def test_catalog_covers_every_feature_and_reason_with_verification() -> None:
    catalog = load_catalog()

    assert set(catalog["features"]) == set(FEATURE_FILES)
    assert set(catalog["reason_families"]) == REASON_CODES
    for entry in catalog["reason_families"].values():
        assert entry["steps"]
        assert entry["verification"]
        assert entry["confirmed_safe_state"]


def test_reason_classification_is_semantic_and_unknown_is_honest() -> None:
    assert (
        reason_code_for(
            HarnessTransportError("socket closed"),
            feature="harnesses",
            event_code="harnesses.turn.failed",
        )
        == "transport_closed"
    )
    assert (
        reason_code_for(
            RuntimeError("not classified"),
            feature="chat",
            event_code="chat.internal.failed",
        )
        == "unknown_internal_fault"
    )


def test_incident_resolution_groups_wrappers_and_keeps_core_root_first() -> None:
    core = {
        "schema": "nebula.diagnostic/v1",
        "timestamp": "2026-07-16T01:19:28.500Z",
        "sequence": 4,
        "level": "ERROR",
        "source": "core",
        "feature": "harnesses",
        "event_code": "harnesses.turn.runtime_failed",
        "message": "The harness runtime reported a turn failure.",
        "error_id": "err_shared",
        "request_id": "req_shared",
        "reason_code": "transport_closed",
        "operator_detail": "Codex app-server closed stdout before turn completion.",
        "impact": "The harness turn did not complete.",
        "remediation_id": "harnesses.transport_closed",
        "retryable": True,
        "metadata": {
            "entity_type": "harness_turn",
            "entity_id": "turn-1",
            "transport": "stdio",
        },
    }
    wrapper = {
        **core,
        "timestamp": "2026-07-16T01:19:28.603Z",
        "sequence": 1,
        "source": "browser",
        "feature": "interface",
        "event_code": "interface.sessions_page.caught_failure_13",
        "message": "A handled interface operation failed.",
    }

    [incident] = resolve_incidents([wrapper, core])

    assert incident.error_id == "err_shared"
    assert incident.primary["source"] == "core"
    assert incident.guidance.cause == (
        "Codex app-server closed stdout before turn completion."
    )
    assert len(incident.related_records) == 1
    assert incident.actions[-1].enabled is True
