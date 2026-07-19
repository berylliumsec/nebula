from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import BootstrapStateRow, Database
from nebula.v3.domain import Engagement, RunnerProfile, ScopePolicy
from nebula.v3.sandbox import ContainerSandboxRunner
from nebula.v3.setup import (
    ImagePreparationCancellationRequest,
    ImagePreparationRequest,
    RunnerSelectionRequest,
    SetupService,
    SetupServiceError,
    bootstrap_scratch_project,
)
from nebula.v3.storage import NebulaStore
from nebula.v3.runtime_platform import (
    KALI_RUNTIME_METADATA_SCHEMA,
    RuntimePlatform,
    RuntimePlatformError,
    _runner_profile_fingerprint,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _platform(tmp_path: Path, store: NebulaStore) -> RuntimePlatform:
    return RuntimePlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core",
        execution_enabled=True,
    )


def _upgrade_to(engine, revision: str) -> None:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[2] / "src" / "nebula" / "v3" / "migrations"),
    )
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_fresh_database_creates_scratch_once_and_never_recreates_after_delete(
    tmp_path,
):
    store = NebulaStore(tmp_path / "fresh.db")

    assert bootstrap_scratch_project(store) == "scratch-project"
    scratch = store.get(Engagement, "scratch-project")
    assert scratch.name == "Scratch Project"
    assert scratch.metadata == {
        "created_by": "system:bootstrap",
        "bootstrap_kind": "scratch_project_v1",
    }
    assert scratch.scope_policy_id == "scope:scratch-project"
    default_scope = store.get(ScopePolicy, scratch.scope_policy_id)
    assert default_scope.engagement_id == scratch.id
    assert default_scope.allowed_cidrs == []
    assert default_scope.allowed_domains == []
    assert default_scope.allowed_urls == []
    assert default_scope.allowed_ports == []
    assert default_scope.local_only is False
    assert default_scope.max_concurrency == 1
    assert bootstrap_scratch_project(store) == scratch.id
    assert store.count(Engagement) == 1

    store.delete(Engagement, scratch.id, expected_revision=scratch.revision)

    assert bootstrap_scratch_project(store) is None
    assert store.count(Engagement) == 0


def test_import_content_arriving_before_first_launch_suppresses_scratch(tmp_path):
    store = NebulaStore(tmp_path / "import.db")
    imported = store.create(Engagement(name="Imported assessment"))

    assert bootstrap_scratch_project(store) is None
    assert [item.id for item in store.list_entities(Engagement)] == [imported.id]

    store.delete(Engagement, imported.id, expected_revision=imported.revision)
    assert bootstrap_scratch_project(store) is None


def test_existing_pre_bootstrap_database_is_not_treated_as_a_fresh_install(tmp_path):
    path = tmp_path / "upgrade.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    _upgrade_to(engine, "0003_operation_events")
    engine.dispose()

    store = NebulaStore(Database(path))

    with store.database.session() as session:
        marker = session.get(BootstrapStateRow, "scratch_project_v1")
        assert marker is not None
        assert marker.status == "complete"
        assert marker.engagement_id is None
    assert bootstrap_scratch_project(store) is None
    assert store.count(Engagement) == 0


def test_concurrent_scratch_bootstrap_is_idempotent(tmp_path):
    store = NebulaStore(tmp_path / "concurrent.db")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bootstrap_scratch_project(store), range(8)))

    assert results == ["scratch-project"] * 8
    assert store.count(Engagement) == 1


def test_concurrent_fresh_store_constructors_create_exactly_one_scratch(tmp_path):
    database_path = tmp_path / "concurrent-constructors.db"
    worker_count = 4
    gate_path = tmp_path / "constructor-gate"
    ready_paths = [
        tmp_path / f"constructor-ready-{index}" for index in range(worker_count)
    ]
    worker = """
import sys
import time
from pathlib import Path

from nebula.v3.setup import bootstrap_scratch_project
from nebula.v3.storage import NebulaStore

database_path, gate_path, ready_path = sys.argv[1:]
Path(ready_path).touch()
gate = Path(gate_path)
while not gate.exists():
    time.sleep(0.005)
store = NebulaStore(database_path)
try:
    print(bootstrap_scratch_project(store), flush=True)
finally:
    store.database.dispose()
"""
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, environment.get("PYTHONPATH")) if value
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker,
                str(database_path),
                str(gate_path),
                str(ready_path),
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready_path in ready_paths
    ]
    try:
        deadline = time.monotonic() + 20
        while not all(path.exists() for path in ready_paths):
            if time.monotonic() >= deadline:
                gate_path.touch()
                pytest.fail("concurrent constructor workers did not become ready")
            time.sleep(0.01)
        gate_path.touch()
        outputs = [process.communicate(timeout=30) for process in processes]
    finally:
        gate_path.touch()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()

    failures = [
        (process.returncode, stderr)
        for process, (_, stderr) in zip(processes, outputs, strict=True)
        if process.returncode != 0
    ]
    assert failures == []
    assert [stdout.strip() for stdout, _ in outputs] == [
        "scratch-project"
    ] * worker_count
    store = NebulaStore(database_path)
    try:
        assert [item.id for item in store.list_entities(Engagement)] == [
            "scratch-project"
        ]
        with store.database.session() as session:
            markers = session.query(BootstrapStateRow).all()
        assert len(markers) == 1
        assert markers[0].key == "scratch_project_v1"
        assert markers[0].status == "complete"
        assert markers[0].engagement_id == "scratch-project"
    finally:
        store.database.dispose()


def test_interrupted_fresh_migration_preserves_scratch_eligibility(
    tmp_path, monkeypatch
):
    path = tmp_path / "interrupted.db"

    def interrupt_migration(_database: Database) -> None:
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(Database, "_run_alembic_migrations", interrupt_migration)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        Database(path)

    monkeypatch.undo()
    store = NebulaStore(path)
    try:
        assert bootstrap_scratch_project(store) == "scratch-project"
        assert store.count(Engagement) == 1
    finally:
        store.database.dispose()


def test_setup_status_contract_is_authenticated_and_model_optional(tmp_path):
    store = NebulaStore(tmp_path / "api.db")
    app = create_app(store, auth_token=TOKEN, bootstrap_workspace=True)
    client = TestClient(app)

    assert client.get("/api/v1/setup/status").status_code == 401
    response = client.get("/api/v1/setup/status", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "application_stage": "ready",
        "stage_detail": "Nebula is ready.",
        "stage_started_at": None,
        "retryable": False,
        "recovery_actions": [],
        "core": {"status": "ready", "detail": None},
        "scratch_project_id": "scratch-project",
        "terminal": {
            "status": "disabled",
            "runner_profile_id": None,
            "candidates": [],
            "image_preparation": {
                "phase": "not_started",
                "operation_id": None,
                "project_id": None,
                "progress_percent": None,
                "progress_indeterminate": False,
                "can_cancel": False,
                "can_retry": False,
                "image_digest": None,
                "started_at": None,
                "completed_at": None,
                "detail": None,
            },
            "detail": "Container terminal execution is disabled in this Core.",
        },
        "assistant": {
            "status": "needs_model",
            "provider_profile_id": None,
            "detail": (
                "Assistant setup is optional; Terminal is available independently."
            ),
        },
    }


def test_create_app_does_not_bootstrap_embedded_stores_unless_requested(tmp_path):
    store = NebulaStore(tmp_path / "embedded.db")

    create_app(store, auth_token=TOKEN)

    assert store.count(Engagement) == 0


def test_prepared_runtime_survives_health_refresh_but_not_runner_config_change(
    tmp_path,
):
    store = NebulaStore(tmp_path / "runner-fingerprint.db")
    engagement = store.create(Engagement(name="Runner fingerprint"))
    profile = store.create(
        RunnerProfile(
            id="local",
            name="Local Docker",
            runtime="docker",
            executable="/usr/bin/docker",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )
    )
    platform = _platform(tmp_path, store)
    platform.runtime_metadata_path.write_text(
        json.dumps(
            {
                "schema": KALI_RUNTIME_METADATA_SCHEMA,
                "image_digest": "sha256:" + "a" * 64,
                "runner_profile_id": profile.id,
                "runner_profile_revision": profile.revision,
                "runner_profile_fingerprint": _runner_profile_fingerprint(profile),
                "binary_inventory": [
                    {"name": "bash", "path": "/usr/bin/bash", "version": "5"}
                ],
            }
        ),
        encoding="utf-8",
    )
    refreshed = store.update(
        RunnerProfile,
        profile.id,
        {"last_health_at": "2026-07-18T12:00:00Z", "last_health_detail": "ready"},
        expected_revision=profile.revision,
    )

    assert (
        platform.resolve_operator_runtime(
            engagement.id, "bash", network=False
        ).profile.revision
        == refreshed.revision
    )

    changed = store.update(
        RunnerProfile,
        refreshed.id,
        {"context": "alternate-rootless-context"},
        expected_revision=refreshed.revision,
    )
    with pytest.raises(RuntimePlatformError, match="runner has changed"):
        platform.resolve_operator_runtime(engagement.id, "bash", network=False)
    assert changed.revision > profile.revision


def test_exactly_one_verified_fixed_runtime_is_persisted_as_local(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "trusted" / "podman"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o755)
    untrusted = tmp_path / "path" / "docker"
    untrusted.parent.mkdir()
    untrusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    untrusted.chmod(0o755)
    monkeypatch.setenv("PATH", str(untrusted.parent))
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", (runtime,))

    async def healthy(runner):
        assert runner.runtime == str(runtime)
        return True, "approved local rootless Podman runner is available"

    monkeypatch.setattr(ContainerSandboxRunner, "available", healthy)
    store = NebulaStore(tmp_path / "runtime.db")
    service = SetupService(store, _platform(tmp_path, store))

    status = asyncio.run(service.refresh())

    assert status.terminal.status == "preparing_image"
    assert status.terminal.image_preparation.phase == "not_started"
    assert status.terminal.runner_profile_id == "local"
    assert [candidate.executable for candidate in status.terminal.candidates] == [
        str(runtime)
    ]
    profile = store.get(RunnerProfile, "local")
    assert profile.executable == str(runtime)
    assert profile.healthy is True


def test_multiple_healthy_fixed_runtimes_require_selection_and_persist_nothing(
    tmp_path, monkeypatch
):
    runtimes = (tmp_path / "trusted" / "podman", tmp_path / "trusted" / "docker")
    runtimes[0].parent.mkdir()
    for runtime in runtimes:
        runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime.chmod(0o755)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", runtimes)

    async def healthy(_runner):
        return True, "verified local runtime"

    monkeypatch.setattr(ContainerSandboxRunner, "available", healthy)
    store = NebulaStore(tmp_path / "ambiguous.db")
    service = SetupService(store, _platform(tmp_path, store))

    status = asyncio.run(service.refresh())

    assert status.terminal.status == "needs_runner"
    assert {candidate.runtime.value for candidate in status.terminal.candidates} == {
        "docker",
        "podman",
    }
    assert store.count(RunnerProfile) == 0


def test_runtime_detection_never_searches_path_or_environment_override(
    tmp_path, monkeypatch
):
    untrusted = tmp_path / "path" / "docker"
    untrusted.parent.mkdir()
    untrusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    untrusted.chmod(0o755)
    monkeypatch.setenv("PATH", str(untrusted.parent))
    monkeypatch.setenv("NEBULA_V3_CONTAINER_RUNTIME", str(untrusted))
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", ())
    store = NebulaStore(tmp_path / "no-path.db")
    service = SetupService(store, _platform(tmp_path, store))

    status = asyncio.run(service.refresh())

    assert status.terminal.status == "needs_runner"
    assert status.terminal.candidates == []
    assert store.count(RunnerProfile) == 0


def test_fixed_candidate_selection_is_idempotent_and_never_accepts_a_path(
    tmp_path, monkeypatch
):
    runtimes = (tmp_path / "trusted" / "podman", tmp_path / "trusted" / "docker")
    runtimes[0].parent.mkdir()
    for runtime in runtimes:
        runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime.chmod(0o755)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", runtimes)

    async def healthy(_runner):
        return True, "verified local runtime"

    monkeypatch.setattr(ContainerSandboxRunner, "available", healthy)
    store = NebulaStore(tmp_path / "selection.db")
    service = SetupService(store, _platform(tmp_path, store))

    async def scenario():
        detected = await service.refresh()
        selected_candidate = next(
            candidate
            for candidate in detected.terminal.candidates
            if candidate.runtime.value == "podman"
        )
        assert selected_candidate.candidate_id is not None
        selected = await service.select_runner(
            RunnerSelectionRequest(candidate_id=selected_candidate.candidate_id)
        )
        repeated = await service.select_runner(
            RunnerSelectionRequest(candidate_id=selected_candidate.candidate_id)
        )
        with pytest.raises(SetupServiceError, match="no longer available"):
            await service.select_runner(
                RunnerSelectionRequest(candidate_id="fixed:" + "f" * 32)
            )
        return selected_candidate, selected, repeated

    candidate, selected, repeated = asyncio.run(scenario())

    assert selected.accepted is True
    assert selected.idempotent is False
    assert selected.setup.terminal.status == "preparing_image"
    assert repeated.setup.terminal.status == "preparing_image"
    assert selected.setup.terminal.runner_profile_id == "local"
    assert repeated.idempotent is True
    profile = store.get(RunnerProfile, "local")
    assert profile.executable == candidate.executable == str(runtimes[0])
    assert profile.context == candidate.context


def test_setup_event_replay_is_monotonic_snapshot_bounded_and_reports_gaps(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", ())
    store = NebulaStore(tmp_path / "events.db")
    service = SetupService(store, _platform(tmp_path, store), event_retention=3)

    async def refresh_repeatedly():
        await service.refresh()
        await service.refresh()
        await service.refresh()

    asyncio.run(refresh_repeatedly())
    replay = service.replay_events(0)

    assert replay.truncated is True
    assert len(replay.events) == 3
    sequences = [event.sequence for event in replay.events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert replay.oldest_sequence == sequences[0]
    assert replay.latest_sequence == sequences[-1]
    assert all(event.snapshot.terminal.candidates == [] for event in replay.events)
    assert replay.events[-1].snapshot.terminal.status == "needs_runner"


def test_image_preparation_reports_phases_can_cancel_and_retry(tmp_path, monkeypatch):
    store = NebulaStore(tmp_path / "preparation.db")
    project = store.create(Engagement(name="Image preparation"))
    store.create(
        RunnerProfile(
            id="local",
            name="Local Podman",
            runtime="podman",
            executable="/usr/bin/podman",
            platform="linux/amd64",
            isolation="rootless",
            healthy=True,
        )
    )
    platform = _platform(tmp_path, store)
    service = SetupService(store, platform)
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0

    async def resolve(project_id, *, on_progress=None):
        nonlocal calls
        assert project_id == project.id
        if on_progress is not None:
            await on_progress("Downloading the official Kali base image.")
        calls += 1
        if calls == 1:
            raise RuntimePlatformError("image registry is unavailable")
        if calls == 2:
            second_started.set()
            await second_release.wait()
        return SimpleNamespace(
            image=SimpleNamespace(
                digest="sha256:" + "a" * 64,
                detail="verified cached workstation image",
            )
        )

    monkeypatch.setattr(platform, "resolve_human_terminal_runtime", resolve)

    async def wait_for_phase(expected):
        for _ in range(100):
            status = await service.status()
            if status.terminal.image_preparation.phase == expected:
                return status
            await asyncio.sleep(0)
        pytest.fail(f"setup never reached image phase {expected}")

    async def scenario():
        first = await service.prepare_image(
            ImagePreparationRequest(project_id=project.id)
        )
        failed = await wait_for_phase("error")
        assert failed.terminal.image_preparation.can_retry is True

        retry = await service.retry_image_preparation(
            ImagePreparationRequest(project_id=project.id)
        )
        await second_started.wait()
        duplicate = await service.retry_image_preparation(
            ImagePreparationRequest(project_id=project.id)
        )
        cancelled = await service.cancel_image_preparation(
            ImagePreparationCancellationRequest(operation_id=retry.operation_id)
        )
        repeated_cancel = await service.cancel_image_preparation(
            ImagePreparationCancellationRequest(operation_id=retry.operation_id)
        )

        final_retry = await service.retry_image_preparation(ImagePreparationRequest())
        ready = await wait_for_phase("ready")
        return (
            first,
            retry,
            duplicate,
            cancelled,
            repeated_cancel,
            final_retry,
            ready,
        )

    (
        first,
        retry,
        duplicate,
        cancelled,
        repeated_cancel,
        final_retry,
        ready,
    ) = asyncio.run(scenario())

    assert first.operation_id != retry.operation_id != final_retry.operation_id
    assert duplicate.idempotent is True
    assert duplicate.operation_id == retry.operation_id
    assert cancelled.accepted is True
    assert cancelled.setup.terminal.image_preparation.phase == "cancelled"
    assert cancelled.setup.terminal.status == "preparing_image"
    assert repeated_cancel.idempotent is True
    assert ready.terminal.image_preparation.image_digest == "sha256:" + "a" * 64
    assert ready.terminal.image_preparation.progress_percent == 100
    assert ready.terminal.status == "ready"
    reasons = [event.reason.value for event in service.replay_events(0).events]
    progress_details = [
        event.snapshot.terminal.image_preparation.detail
        for event in service.replay_events(0).events
        if event.reason.value == "image_preparation_progress"
    ]
    assert "image_preparation_progress" in reasons
    assert "Downloading the official Kali base image." in progress_details
    assert "image_preparation_error" in reasons
    assert "image_preparation_cancelling" in reasons
    assert "image_preparation_cancelled" in reasons
    assert "image_preparation_ready" in reasons


def test_setup_control_api_and_sse_are_authenticated_and_path_closed(
    tmp_path, monkeypatch
):
    runtimes = (tmp_path / "trusted" / "podman", tmp_path / "trusted" / "docker")
    runtimes[0].parent.mkdir()
    for runtime in runtimes:
        runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        runtime.chmod(0o755)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", runtimes)

    async def healthy(_runner):
        return True, "verified local runtime"

    monkeypatch.setattr(ContainerSandboxRunner, "available", healthy)
    store = NebulaStore(tmp_path / "setup-api.db")
    platform = _platform(tmp_path, store)

    async def resolve(_project_id, *, on_progress=None):
        if on_progress is not None:
            await on_progress("Verifying the prepared Kali runtime image.")
        return SimpleNamespace(
            image=SimpleNamespace(
                digest="sha256:" + "b" * 64,
                detail="verified cached workstation image",
            )
        )

    monkeypatch.setattr(platform, "resolve_human_terminal_runtime", resolve)
    app = create_app(
        store,
        auth_token=TOKEN,
        bootstrap_workspace=True,
        tool_platform=platform,
    )

    with TestClient(app) as client:
        assert client.get("/api/v1/setup/events?follow=false").status_code == 401
        assert (
            client.post(
                "/api/v1/setup/runtime/select",
                json={"candidate_id": "fixed:" + "a" * 32},
            ).status_code
            == 401
        )

        refreshed = client.post("/api/v1/setup/runtime/refresh", headers=AUTH).json()
        candidate_id = refreshed["terminal"]["candidates"][0]["candidate_id"]
        rejected_path = client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id, "executable": "/tmp/evil"},
        )
        assert rejected_path.status_code == 422

        selected = client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id},
        )
        assert selected.status_code == 200
        assert selected.json()["setup"]["terminal"]["runner_profile_id"] == "local"
        repeated = client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id},
        )
        assert repeated.json()["idempotent"] is True

        prepared = client.post(
            "/api/v1/setup/image/prepare",
            headers=AUTH,
            json={"project_id": "scratch-project"},
        )
        assert prepared.status_code == 200
        operation_id = prepared.json()["operation_id"]
        for _ in range(100):
            status = client.get("/api/v1/setup/status", headers=AUTH).json()
            if status["terminal"]["image_preparation"]["phase"] == "ready":
                break
        else:
            pytest.fail("image preparation API did not reach ready")

        retried = client.post(
            "/api/v1/setup/image/retry",
            headers=AUTH,
            json={"project_id": "scratch-project"},
        )
        assert retried.json()["idempotent"] is True
        cancelled_ready = client.post(
            "/api/v1/setup/image/cancel",
            headers=AUTH,
            json={"operation_id": operation_id},
        )
        assert cancelled_ready.json()["idempotent"] is True

        events = client.get("/api/v1/setup/events?follow=false", headers=AUTH)
        assert events.status_code == 200
        assert events.headers["content-type"].startswith("text/event-stream")
        ids = [
            int(line.removeprefix("id: "))
            for line in events.text.splitlines()
            if line.startswith("id: ")
        ]
        payloads = [
            json.loads(line.removeprefix("data: "))
            for line in events.text.splitlines()
            if line.startswith("data: ")
        ]
        assert ids == sorted(ids)
        assert len(ids) == len(set(ids)) == len(payloads)
        assert all(
            payload["sequence"] == sequence for payload, sequence in zip(payloads, ids)
        )
        assert all("snapshot" in payload for payload in payloads)

        resumed = client.get(
            "/api/v1/setup/events?follow=false",
            headers={**AUTH, "Last-Event-ID": str(ids[-2])},
        )
        resumed_ids = [
            int(line.removeprefix("id: "))
            for line in resumed.text.splitlines()
            if line.startswith("id: ")
        ]
        assert resumed_ids == [ids[-1]]
