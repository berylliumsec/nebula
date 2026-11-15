"""Records written before the attack workflows were retired still load."""

import pytest

from nebula.v3.domain import BrowserCommand, BrowserTrafficExchange


def _traffic(**extra):
    return {
        "engagement_id": "project-1",
        "session_id": "session-1",
        "tab_id": "tab-1",
        "identity_id": "identity-1",
        "method": "GET",
        "url": "https://app.example.test/",
        "scope_state": "in_scope",
        "scope_policy_id": "policy-1",
        "scope_policy_revision": 1,
        **extra,
    }


@pytest.mark.parametrize("value", [None, "assessment-legacy-1"])
def test_traffic_written_with_an_assessment_still_loads(value):
    record = BrowserTrafficExchange.model_validate(_traffic(assessment_id=value))
    assert record.assessment_id == value
    assert BrowserTrafficExchange.model_validate(_traffic()).assessment_id is None


def test_command_keeps_the_retired_assessment_field_optional():
    assert BrowserCommand.model_fields["assessment_id"].is_required() is False
