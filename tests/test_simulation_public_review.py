"""The host command reviewer that asks local Core to review on Butler's harnesses."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "simulation" / "nebula_public_review.py"
SPEC = importlib.util.spec_from_file_location("nebula_public_review", SOURCE)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)

VERDICT = {"decision": "allow", "category": "none", "reviewed_files": ["0001"]}


def run(monkeypatch, capsys, answers, stdin=None):
    calls = []

    def call(origin, token, method, path, body=None, key=None):
        calls.append((method, path, json.loads(body) if body else None, key))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(review, "_call", call)
    monkeypatch.setattr(review, "service_token", lambda _path: "synthetic-token")
    monkeypatch.setattr(sys, "argv", ["nebula_public_review.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        stdin if stdin is not None else json.dumps({"prompt": "Review this.", "schema": {}})))
    code = review.main()
    out = capsys.readouterr()
    return code, out.out, out.err, calls


def test_unavailable_profile_moves_to_the_next_and_prints_its_verdict(monkeypatch, capsys):
    code, out, err, calls = run(monkeypatch, capsys, [
        (200, {}, {"attempt": 0, "unavailable": "Codex (a): sign-in expired", "next_attempt": 1}),
        (202, {"X-Nebula-Command-Id": "command-1"}, {"state": "accepted"}),
        (200, {}, {"attempt": 1, "verdict": VERDICT, "reviewer": {"name": "Claude", "model": "opus"}}),
    ])
    assert code == 0, err
    assert json.loads(out) == VERDICT
    assert "Reviewed on Claude (opus)" in err
    assert [(method, path) for method, path, _, _ in calls] == [
        ("POST", review.ROUTE), ("POST", review.ROUTE),
        ("GET", "/api/v1/runtime/requests/command-1/response"),
    ]
    assert [body["attempt"] for _, _, body, _ in calls[:2]] == [0, 1]
    assert calls[0][2]["prompt"] == "Review this."
    assert calls[0][3] != calls[1][3] and calls[0][3].startswith("public-review-")


def test_no_available_profile_fails_with_every_reason(monkeypatch, capsys):
    code, out, err, _ = run(monkeypatch, capsys, [
        (200, {}, {"unavailable": "a: disabled", "next_attempt": 1}),
        (200, {}, {"unavailable": "b: timed out", "next_attempt": None}),
    ])
    assert code == 3 and out == ""
    assert err.strip().endswith("no Butler reviewer harness was available: a: disabled; b: timed out")


@pytest.mark.parametrize("failure", [
    review.ReviewFailed("Core is not reachable at http://127.0.0.1:8000 (refused)"),
    review.ReviewFailed("Core answered HTTP 422: set NEBULA_BUTLER_PUBLIC_REVIEW_HARNESS_PROFILE_IDS"),
])
def test_core_failure_is_reported_on_one_line(monkeypatch, capsys, failure):
    code, out, err, _ = run(monkeypatch, capsys, [failure])
    assert code == 3 and out == ""
    assert err.strip() == f"Butler review unavailable: {failure}"


def test_missing_prompt_is_refused_before_calling_core(monkeypatch, capsys):
    code, _, err, calls = run(monkeypatch, capsys, [], stdin=json.dumps({"schema": {}}))
    assert code == 3 and calls == [] and "no review prompt" in err


def test_only_local_core_origins_are_accepted(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["nebula_public_review.py", "--origin", "https://example.invalid"])
    with pytest.raises(SystemExit):
        review.main()
