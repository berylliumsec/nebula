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
        monkeypatch,
        [TimeoutError("timed out"), URLError("refused"), b'{"health": "ready"}'],
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


def _transient_client(monkeypatch, outcomes):
    """A client whose transient retries run on a fake clock."""
    client, calls = _client(monkeypatch, outcomes)
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gate.time, "sleep", sleep)
    return client, calls, sleeps


def _http(code):
    return HTTPError("http://core", code, "answer", {}, None)


def test_a_promotion_read_rides_out_a_busy_core(monkeypatch, capsys):
    client, calls, sleeps = _transient_client(
        monkeypatch, [_http(500), _http(500), b'{"revision": 7}']
    )
    assert client.read("deployment/status") == {"revision": 7}
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]
    log = capsys.readouterr().err
    assert log.count("Core read deployment/status failed (HTTP 500)") == 2


@pytest.mark.parametrize(
    "failure",
    [
        _http(502),
        _http(503),
        _http(504),
        TimeoutError("timed out"),
        URLError(ConnectionRefusedError("refused")),
        ConnectionResetError("reset"),
    ],
)
def test_a_promotion_read_retries_each_transient_failure(monkeypatch, failure):
    client, calls, _sleeps = _transient_client(monkeypatch, [failure, b"{}"])
    assert client.read("deployment/status") == {}
    assert len(calls) == 2


def test_a_persistent_500_fails_after_the_bound_with_the_resume_hint(monkeypatch):
    client, calls, sleeps = _transient_client(monkeypatch, [_http(500)] * 50)
    with pytest.raises(gate.CoreUnavailable, match="re-run `nebula_deployer promote`"):
        client.read("deployment/status")
    assert gate.TRANSIENT_BUDGET_SECONDS / 2 <= sum(sleeps)
    assert sum(sleeps) <= gate.TRANSIENT_BUDGET_SECONDS
    assert len(calls) == len(sleeps) + 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 422])
def test_a_promotion_read_fails_at_once_on_a_4xx(monkeypatch, code):
    client, calls, sleeps = _transient_client(monkeypatch, [_http(code), b"{}"])
    with pytest.raises(HTTPError) as raised:
        client.read("deployment/operations/x")
    assert raised.value.code == code
    assert len(calls) == 1 and sleeps == []


def test_plain_get_still_answers_503_to_its_caller(monkeypatch):
    # An ownerless project's workspace answers 503; ``get`` callers read it.
    client, calls, _sleeps = _transient_client(monkeypatch, [_http(503), b"{}"])
    with pytest.raises(HTTPError):
        client.get("engagements/x/workspace/reset-status")
    assert len(calls) == 1
