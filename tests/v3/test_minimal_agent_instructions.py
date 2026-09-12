from __future__ import annotations

from nebula.v3.application_model.workflow import BROWSER_MODEL_WORKFLOW
from nebula.v3.chat import (
    _CHAT_BASE_INSTRUCTIONS,
    _CHAT_INSTRUCTIONS,
    _CHAT_TOOL_INSTRUCTIONS,
    _CHAT_TOOL_RESULT_INSTRUCTIONS,
    _RETRIEVAL_AGENT_INSTRUCTIONS,
)
from nebula.v3.domain import HarnessNativeCapabilities, HarnessSession
from nebula.v3.harnesses import _harness_developer_instructions
from nebula.v3.writing_ai import _PURPOSE_INSTRUCTIONS


FORBIDDEN_POLICY_PHRASES = (
    "untrusted",
    "trusted",
    "never follow",
    "data only",
    "ignore instructions",
    "only use facts",
)


def test_harness_instructions_only_route_nebula_workspace_and_tools() -> None:
    instructions = _harness_developer_instructions(
        HarnessSession(
            engagement_id="engagement",
            harness_profile_id="profile",
            model="model",
        ),
        HarnessNativeCapabilities(),
        vendor="Codex",
    )

    assert instructions.startswith("Nebula Codex session.")
    assert "Project operations use the supplied Nebula tools" in instructions
    assert "project root is cwd '.'" in instructions
    assert "browser.companion" in instructions
    assert "model.transact" in instructions
    assert len(instructions) < 400
    assert not any(
        phrase in instructions.lower() for phrase in FORBIDDEN_POLICY_PHRASES
    )


def test_chat_instructions_only_define_turn_protocol() -> None:
    instructions = "\n".join(
        (
            _CHAT_BASE_INSTRUCTIONS,
            _CHAT_INSTRUCTIONS,
            _CHAT_TOOL_INSTRUCTIONS,
            _CHAT_TOOL_RESULT_INSTRUCTIONS,
            _RETRIEVAL_AGENT_INSTRUCTIONS,
        )
    )

    assert "finish_response" in instructions
    assert "[source_id:chunk_id]" in instructions
    assert "JSON `queries` array" in instructions
    assert not any(
        phrase in instructions.lower() for phrase in FORBIDDEN_POLICY_PHRASES
    )


def test_browser_workflow_only_names_optional_capabilities() -> None:
    assert "browser.companion" in BROWSER_MODEL_WORKFLOW
    assert "model.transact" in BROWSER_MODEL_WORKFLOW
    assert len(BROWSER_MODEL_WORKFLOW) < 300
    assert not any(
        phrase in BROWSER_MODEL_WORKFLOW.lower() for phrase in FORBIDDEN_POLICY_PHRASES
    )


def test_writing_instructions_only_name_the_requested_output() -> None:
    assert _PURPOSE_INSTRUCTIONS == {
        "note": "Write an editable analyst note.",
        "report_summary": "Write an executive summary.",
        "report_section": "Write an editable report section.",
        "code_suggestion": "Return an editable code suggestion without Markdown fences.",
    }
