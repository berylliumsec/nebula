"""Core reads made by the simulation deployer survive a slow or busy Core."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "deploy_gate_reads", SCRIPT_DIR / "nebula_deploy_gate.py"
)
assert spec is not None and spec.loader is not None
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _client(monkeypatch, outcomes):
    calls = []

    def urlopen(request, timeout):
        calls.append(timeout)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(outcome)

    monkeypatch.setattr(gate, "urlopen", urlopen)
    monkeypatch.setattr(gate.time, "sleep", lambda _seconds: None)
    return gate.CoreClient("http://127.0.0.1:8000", "token"), calls


def test_a_read_that_times_out_is_asked_again(monkeypatch):
    client, calls = _client(
        monkeypatch, [TimeoutError("timed out"), URLError("refused"), b'{"health": "ready"}']
    )
    assert client.get("deployment/status") == {"health": "ready"}
    assert calls == [gate.READ_TIMEOUT_SECONDS] * 3
    assert gate.READ_TIMEOUT_SECONDS >= 60


def test_a_read_that_keeps_timing_out_raises_after_its_attempts(monkeypatch):
    client, calls = _client(
        monkeypatch, [TimeoutError("timed out")] * gate.READ_ATTEMPTS
    )
    with pytest.raises(TimeoutError):
        client.get("deployment/status")
    assert len(calls) == gate.READ_ATTEMPTS


def test_an_http_answer_is_not_asked_again(monkeypatch):
    error = HTTPError("http://core", 404, "missing", {}, None)
    client, calls = _client(monkeypatch, [error, b"{}"])
    with pytest.raises(HTTPError):
        client.get("engagements/x/hold")
    assert len(calls) == 1
