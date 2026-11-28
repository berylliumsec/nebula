"""The operator chooses what Butler's scheduled and event-fired checks are told."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

from types import SimpleNamespace

import pytest

from nebula.v3 import butler_recovery
from nebula.v3.butler_recovery import ButlerRecoveryAgent

MANDATE = "Decide and act for the operator."


def _agent(tmp_path) -> ButlerRecoveryAgent:
    return ButlerRecoveryAgent(
        None,  # type: ignore[arg-type]
        provider_factory=lambda _: None,
        harness_runtime=None,
        artifact_store=None,
        work=None,
        memory=object(),
        instructions_root=tmp_path,
    )


def _write(tmp_path, text: str = MANDATE) -> None:
    root = tmp_path / ".butler"
    root.mkdir(exist_ok=True)
    (root / "BUTLER.md").write_text(text)


def test_event_checks_keep_core_prompt_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("NEBULA_BUTLER_CHECK_INSTRUCTIONS", raising=False)
    _write(tmp_path)
    text = _agent(tmp_path)._event_instructions()
    assert text == butler_recovery._INSTRUCTIONS
    assert MANDATE not in text


def test_event_checks_use_butler_md_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_BUTLER_CHECK_INSTRUCTIONS", "butler_md")
    _write(tmp_path)
    text = _agent(tmp_path)._event_instructions()
    assert MANDATE in text
    assert text.startswith(butler_recovery._EVENT_FRAME)
    for restriction in (
        "project-scoped change",
        "stay visible for the operator",
        "Do not expand execution or network scope",
        "requires operator action",
    ):
        assert restriction not in text


def test_event_checks_fall_back_to_core_prompt_without_butler_md(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBULA_BUTLER_CHECK_INSTRUCTIONS", "butler_md")
    assert _agent(tmp_path)._event_instructions() == butler_recovery._INSTRUCTIONS


@pytest.mark.parametrize(
    ("mode", "frame"),
    [("", "_GLOBAL_INSTRUCTIONS"), ("butler_md", "_SCHEDULED_FRAME")],
)
async def test_scheduled_check_frame_follows_configuration(
    tmp_path, monkeypatch, mode, frame
):
    monkeypatch.setenv("NEBULA_BUTLER_CHECK_INSTRUCTIONS", mode)
    _write(tmp_path)
    agent = _agent(tmp_path)
    loaded = butler_recovery.read_butler_instructions(tmp_path)
    assert loaded is not None

    class Recovery:
        def __init__(self, _store) -> None:
            pass

        async def source_event(self, _case):
            return SimpleNamespace(
                payload={
                    "summary": "check",
                    "metrics": {"instructions_sha256": loaded.sha256},
                }
            )

    monkeypatch.setattr(butler_recovery, "ButlerRecoveryService", Recovery)
    monkeypatch.setattr(
        butler_recovery.ButlerEventPayload,
        "model_validate",
        staticmethod(lambda value: SimpleNamespace(metrics=value["metrics"])),
    )
    case = SimpleNamespace(signal="butler.global_periodic_check")
    text = await agent._global_instructions(case)  # type: ignore[arg-type]
    assert text.startswith(getattr(butler_recovery, frame))
    assert MANDATE in text
    if mode == "butler_md":
        assert "Operator-only actions stay with the operator" not in text
