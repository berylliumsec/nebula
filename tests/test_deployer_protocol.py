"""Live protocol evidence prevents accidental offline supervisor rollback."""

import importlib.util
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
