"""The deployer times each step and can build a release ahead of promotion."""

from __future__ import annotations

import fcntl
import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts/simulation"
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location(
    "simulation_nebula_deployer_prebuild", SCRIPT_DIR / "nebula_deployer.py"
)
assert spec is not None and spec.loader is not None
deployer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployer)

OLD, NEW = "a" * 40, "c" * 40


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_step_timings_record_each_step_and_the_one_that_failed(capsys):
    clock = Clock()
    timings = deployer.StepTimings("prepare", clock=clock)
    with pytest.raises(RuntimeError, match="build broke"):
        with timings.run():
            timings.begin("checkout")
            clock.now += 2.0
            timings.begin("core_build")
            clock.now += 110.04
            raise RuntimeError("build broke")
    assert timings.steps == [
        {"step": "checkout", "seconds": 2.0},
        {"step": "core_build", "seconds": 110.0, "failed": True},
    ]
    assert timings.record()["total_seconds"] == 112.0
    err = capsys.readouterr().err
    assert "prepare: checkout took 2.0s" in err
    assert "prepare: core_build failed after 110.0s" in err


def test_prepare_reports_the_step_it_stopped_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deployer, "git_source", lambda: None)
    monkeypatch.setattr(deployer, "baseline", lambda: OLD)
    monkeypatch.setattr(deployer, "state", lambda: {})
    monkeypatch.setattr(deployer, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(deployer, "candidate", lambda sha: tmp_path / "candidate")
    monkeypatch.setattr(
        deployer,
        "run",
        lambda *args, capture=False, **_kwargs: NEW if "rev-parse" in args else "",
    )

    def missing(directory, *, previous, sha):
        raise deployer.CiEvidenceUnavailable("CI is still running")

    monkeypatch.setattr(deployer, "discover_ci_run_ids", missing)
    with pytest.raises(deployer.CiEvidenceUnavailable):
        deployer.prepare()
    err = capsys.readouterr().err
    assert "prepare: fetch took" in err
    assert "prepare: checkout took" in err
    assert "prepare: ci_evidence failed after" in err


def test_prebuild_waits_for_ci_instead_of_failing(monkeypatch):
    monkeypatch.setattr(deployer, "state", lambda: {})

    def waiting():
        raise deployer.CiEvidenceUnavailable(f"{NEW}: CI run is still in progress")

    monkeypatch.setattr(deployer, "prepare", waiting)
    assert deployer.prebuild() == {
        "status": "waiting_for_ci",
        "reason": f"{NEW}: CI run is still in progress",
    }


def test_prebuild_leaves_an_unfinished_deployment_alone(monkeypatch):
    monkeypatch.setattr(
        deployer,
        "state",
        lambda: {"runtime_deployment": {"state": "running", "version": OLD}},
    )
    monkeypatch.setattr(
        deployer, "prepare", lambda: pytest.fail("prebuild must not prepare")
    )
    result = deployer.prebuild()
    assert result["status"] == "skipped"
    assert result["reason"] == "deployment_in_progress"


def test_prebuild_reports_the_built_release_and_its_timings(monkeypatch):
    monkeypatch.setattr(deployer, "state", lambda: {})
    timings = {
        "steps": [{"step": "core_build", "seconds": 109.0}],
        "total_seconds": 317.0,
    }
    monkeypatch.setattr(
        deployer,
        "prepare",
        lambda: {
            "commit": NEW,
            "previous": OLD,
            "status": "accepted_candidate",
            "timings": timings,
        },
    )
    assert deployer.prebuild() == {
        "status": "prebuilt",
        "commit": NEW,
        "previous": OLD,
        "timings": timings,
    }
    monkeypatch.setattr(
        deployer, "prepare", lambda: {"status": "current", "commit": OLD}
    )
    assert deployer.prebuild() == {"status": "current", "commit": OLD}


def _held_lock(path: Path, action: str):
    holder = path.open("a+")
    assert deployer.acquire_deploy_lock(holder, path, action) is None
    return holder


def test_prebuild_skips_when_a_deployment_holds_the_lock(tmp_path):
    path = tmp_path / ".deploy.lock"
    holder = _held_lock(path, "promote")
    try:
        with path.open("a+") as lock:
            assert deployer.acquire_deploy_lock(lock, path, "prebuild") == "promote"
    finally:
        holder.close()


def test_promote_waits_for_a_running_prebuild(tmp_path):
    path = tmp_path / ".deploy.lock"
    holder = _held_lock(path, "prebuild")
    clock = Clock()
    sleeps: list[float] = []

    def prebuild_finishes(seconds):
        sleeps.append(seconds)
        clock.now += seconds
        holder.close()

    with path.open("a+") as lock:
        assert (
            deployer.acquire_deploy_lock(
                lock, path, "promote", clock=clock, sleep=prebuild_finishes
            )
            is None
        )
        assert '"action": "promote"' in path.read_text()
    assert sleeps == [deployer.LOCK_POLL_SECONDS]


def test_promote_wait_for_a_prebuild_is_bounded(tmp_path):
    path = tmp_path / ".deploy.lock"
    holder = _held_lock(path, "prebuild")
    clock = Clock()

    def stuck(seconds):
        clock.now += seconds * 1000

    try:
        with path.open("a+") as lock:
            with pytest.raises(
                RuntimeError, match=r"\(prebuild\) holds the deploy lock"
            ):
                deployer.acquire_deploy_lock(
                    lock, path, "cycle", clock=clock, sleep=stuck
                )
    finally:
        holder.close()


def test_other_runs_still_refuse_a_busy_lock_at_once(tmp_path):
    path = tmp_path / ".deploy.lock"
    holder = _held_lock(path, "promote")
    try:
        with path.open("a+") as lock:
            with pytest.raises(
                RuntimeError, match=r"\(promote\) holds the deploy lock"
            ):
                deployer.acquire_deploy_lock(
                    lock,
                    path,
                    "cycle",
                    sleep=lambda _s: pytest.fail("must not wait for a promote"),
                )
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        holder.close()
