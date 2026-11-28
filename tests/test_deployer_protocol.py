"""Live protocol evidence prevents accidental offline supervisor rollback."""

import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "protocol_deployer", SCRIPTS / "nebula_deployer.py"
)
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)
OLD, NEW = "a" * 40, "b" * 40
PR_MERGE, PR_HEAD, TREE = "e" * 40, "f" * 40, "9" * 40


def setup(monkeypatch, get):
    effects = []
    record = {"active": OLD, "previous": OLD, "pending": NEW, "promoting": NEW}
    monkeypatch.setattr(deployer, "state", lambda: record)
    monkeypatch.setattr(deployer, "api", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(deployer, "verified_release", lambda _: Path("/inert/release"))
    monkeypatch.setattr(deployer, "baseline", lambda: OLD)
    for name in ("service", "edge", "snapshot", "restore_database_backup"):
        monkeypatch.setattr(
            deployer, name, lambda *args, **kwargs: effects.append(args)
        )
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "protocol guard must precede host commands"
        ),
    )
    return record, effects


@pytest.mark.parametrize(
    "entry", ["promote", "legacy_promote", "rollback", "offline_rollback"]
)
def test_missing_local_marker_never_allows_offline_supervisor_mutation(
    monkeypatch, entry
):
    record, effects = setup(
        monkeypatch, lambda path: {"protocol": "nebula.runtime/v1", "api_version": OLD}
    )
    with pytest.raises(RuntimeError, match="supervised"):
        getattr(deployer, entry)()
    assert not effects and "deployment_mode" not in record


@pytest.mark.parametrize("entry", ["promote", "offline_rollback"])
def test_unavailable_protocol_without_verified_attempt_fails_closed(monkeypatch, entry):
    def unavailable(path):
        raise OSError("inert unavailable listener")

    _, effects = setup(monkeypatch, unavailable)
    with pytest.raises(RuntimeError, match="protocol is unavailable"):
        getattr(deployer, entry)()
    assert not effects


def test_unavailable_failed_legacy_cutover_requires_matching_attempt(monkeypatch):
    def unavailable(path):
        raise OSError("inert unavailable candidate")

    record, effects = setup(monkeypatch, unavailable)
    deployer.require_legacy_protocol(record, attempt=deployer._LegacyAttempt(OLD, NEW))
    with pytest.raises(RuntimeError):
        deployer.require_legacy_protocol(
            record, attempt=deployer._LegacyAttempt(OLD, "c" * 40)
        )
    assert not effects


def test_live_supervisor_vetoes_even_a_prior_legacy_attempt(monkeypatch):
    _, effects = setup(monkeypatch, lambda path: {"protocol": "nebula.runtime/v1"})
    with pytest.raises(RuntimeError, match="supervised"):
        deployer.offline_rollback(_legacy_attempt=deployer._LegacyAttempt(OLD, NEW))
    assert not effects


def test_legacy_discovery_requires_unsupported_route_and_exact_health(monkeypatch):
    def legacy(path):
        if path == "deployment/status":
            raise HTTPError("http://inert", 404, "legacy", {}, None)
        return {"status": "ok", "commit": OLD}

    record, effects = setup(monkeypatch, legacy)
    deployer.require_legacy_protocol(record, expected_version=OLD)
    with pytest.raises(RuntimeError, match="exact legacy"):
        deployer.require_legacy_protocol(record, expected_version=NEW)
    assert not effects


TERMINAL_CLIENT_MATRIX_JOBS = sorted(
    f"terminal-client ({runner}, {version})"
    for runner in ("ubuntu-latest", "macos-15")
    for version in ("3.11", "3.12", "3.13")
)


def test_deployment_post_honours_retry_after_and_resubmits_the_same_operation(
    monkeypatch,
):
    # A stopping supervisor answers 503 with Retry-After while the operation
    # continues; the same operation ID is resubmitted until its receipt exists.
    monkeypatch.setattr(
        deployer,
        "api",
        lambda: SimpleNamespace(origin="http://127.0.0.1:8000", token="inert"),
    )
    sleeps, bodies = [], []
    monkeypatch.setattr(deployer.time, "sleep", sleeps.append)
    receipt = {"operation_id": "op-1", "state": "succeeded", "result": {}}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout):
        bodies.append(request.data)
        if len(bodies) < 3:
            raise HTTPError(
                request.full_url, 503, "restarting", {"Retry-After": "1"}, None
            )
        return Response(json.dumps(receipt).encode())

    monkeypatch.setattr(deployer, "urlopen", urlopen)
    payload = {"operation_id": "op-1", "action": "stage", "parameters": {}}
    assert deployer.deployment_post("deployment/operations", payload) == receipt
    assert sleeps == [1, 1] and len(set(bodies)) == 1

    def refused(request, timeout):
        raise HTTPError(request.full_url, 422, "invalid", {}, None)

    monkeypatch.setattr(deployer, "urlopen", refused)
    with pytest.raises(HTTPError):
        deployer.deployment_post("deployment/operations", payload)
    assert sleeps == [1, 1]


class _Ledger:
    """Core's operation ledger: one dispatch per ID, saved receipts replayed."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.receipts, self.dispatched, self.bodies = {}, [], []

    def urlopen(self, request, timeout):
        payload = json.loads(request.data)
        self.bodies.append(request.data)
        key = payload["operation_id"]
        if key not in self.receipts:
            self.dispatched.append(key)
            self.receipts[key] = {
                "operation_id": key,
                "action": payload["action"],
                "state": "succeeded",
                "result": {"runtime_id": "worker-new"},
            }
        if self.failures:
            raise self.failures.pop(0)
        return _Response(json.dumps(self.receipts[key]).encode())


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _fake_clock(monkeypatch):
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(deployer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deployer.time, "sleep", sleep)
    monkeypatch.setattr(
        deployer,
        "api",
        lambda: SimpleNamespace(origin="http://127.0.0.1:8000", token="inert"),
    )
    return sleeps


@pytest.mark.parametrize(
    "lost",
    [
        ConnectionResetError("reset while the supervisor swapped"),
        TimeoutError("timed out"),
        HTTPError("http://core", 500, "busy", {}, None),
        HTTPError("http://core", 502, "bad gateway", {}, None),
    ],
)
def test_lost_operation_reply_is_read_back_not_applied_twice(monkeypatch, lost):
    # Core applied the operation, but its reply was lost. The deployer sends
    # the same operation ID again and receives the saved receipt.
    sleeps = _fake_clock(monkeypatch)
    ledger = _Ledger([lost])
    monkeypatch.setattr(deployer, "urlopen", ledger.urlopen)
    payload = {"operation_id": "op-2", "action": "prepare_execution", "parameters": {}}
    receipt = deployer.deployment_post("deployment/operations", payload)
    assert receipt["operation_id"] == "op-2" and receipt["state"] == "succeeded"
    assert ledger.dispatched == ["op-2"]
    assert len(ledger.bodies) == 2 and len(set(ledger.bodies)) == 1
    assert sleeps == [2.0]


def test_lost_operation_reply_past_the_bound_names_the_resume(monkeypatch):
    sleeps = _fake_clock(monkeypatch)
    ledger = _Ledger([HTTPError("http://core", 500, "busy", {}, None)] * 50)
    monkeypatch.setattr(deployer, "urlopen", ledger.urlopen)
    payload = {"operation_id": "op-3", "action": "handoff", "parameters": {}}
    with pytest.raises(RuntimeError, match="re-run `nebula_deployer promote`"):
        deployer.deployment_post("deployment/operations", payload)
    assert ledger.dispatched == ["op-3"]
    assert len(set(ledger.bodies)) == 1
    assert sum(sleeps) <= deployer.TRANSIENT_BUDGET_SECONDS


def test_an_operation_refused_with_4xx_is_not_sent_again(monkeypatch):
    sleeps = _fake_clock(monkeypatch)
    ledger = _Ledger([HTTPError("http://core", 409, "other request", {}, None)])
    monkeypatch.setattr(deployer, "urlopen", ledger.urlopen)
    payload = {"operation_id": "op-4", "action": "retire", "parameters": {}}
    with pytest.raises(HTTPError):
        deployer.deployment_post("deployment/operations", payload)
    assert len(ledger.bodies) == 1 and sleeps == []


def test_reconciliation_post_is_not_re_sent_blindly(monkeypatch):
    sleeps = _fake_clock(monkeypatch)
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 500, "busy", {}, None)

    monkeypatch.setattr(deployer, "urlopen", urlopen)
    with pytest.raises(HTTPError):
        deployer.deployment_post("deployment/operations/op-5/reconcile", {})
    assert len(calls) == 1 and sleeps == []


def test_resent_operation_still_running_is_read_until_it_settles(monkeypatch):
    sleeps = _fake_clock(monkeypatch)
    running = {"operation_id": "op-6", "state": "running", "result": {}}
    settled = {"operation_id": "op-6", "state": "succeeded", "result": {"x": 1}}
    replies = [ConnectionResetError("reset"), running]
    reads = [running, settled]

    def urlopen(request, timeout):
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return _Response(json.dumps(reply).encode())

    monkeypatch.setattr(deployer, "urlopen", urlopen)
    monkeypatch.setattr(
        deployer,
        "api",
        lambda: SimpleNamespace(
            origin="http://127.0.0.1:8000",
            token="inert",
            read=lambda path: reads.pop(0),
        ),
    )
    payload = {"operation_id": "op-6", "action": "migrate", "parameters": {}}
    assert deployer.deployment_post("deployment/operations", payload) == settled
    assert not reads and len(sleeps) == 3
