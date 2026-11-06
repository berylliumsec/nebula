from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from tests.v3.async_test_support import AsyncTestClient as TestClient
from sqlalchemy import create_engine

from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import BootstrapStateRow, Database, ProjectCreateRequestRow
from nebula.v3.domain import (
    AutomationProjectPolicy,
    Engagement,
    RunnerProfile,
    ScopePolicy,
)
from nebula.v3.sandbox import (
    ContainerSandboxRunner,
    PreparedContainerImage,
    SandboxError,
    SandboxRequest,
    SandboxWorkspaceAccess,
)
from nebula.v3.setup import (
    ImagePreparationCancellationRequest,
    ImagePreparationRequest,
    RunnerSelectionRequest,
    SetupService,
    SetupServiceError,
    bootstrap_scratch_project,
    create_engagement_with_default_scope,
)
from nebula.v3.storage import ConflictError, NebulaStore
from nebula.v3.runtime_platform import (
    KALI_RUNTIME_METADATA_SCHEMA,
    RuntimePlatform,
    RuntimePlatformError,
    _runner_profile_fingerprint,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_status_keeps_event_loop_responsive_while_snapshot_waits(
    tmp_path, monkeypatch
):
    service = SetupService(NebulaStore(tmp_path / "core.db"), None)
    original_snapshot = service._snapshot
    entered = Event()
    release = Event()

    async def waiting_snapshot():
        entered.set()
        assert release.wait(timeout=2)
        return await original_snapshot()

    monkeypatch.setattr(service, "_snapshot", waiting_snapshot)

    async def scenario():
        task = asyncio.create_task(service.status())
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert not task.done()
        finally:
            release.set()
        await task

    (await scenario())


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


async def test_fresh_database_creates_scratch_once_and_never_recreates_after_delete(
    tmp_path,
):
    store = NebulaStore(tmp_path / "fresh.db")

    assert (await bootstrap_scratch_project(store)) == "scratch-project"
    scratch = await store.get(Engagement, "scratch-project")
    assert scratch.name == "Scratch Project"
    assert scratch.metadata == {
        "created_by": "system:bootstrap",
        "bootstrap_kind": "scratch_project_v1",
    }
    assert scratch.scope_policy_id == "scope:scratch-project"
    default_scope = await store.get(ScopePolicy, scratch.scope_policy_id)
    assert default_scope.engagement_id == scratch.id
    assert default_scope.allowed_cidrs == []
    assert default_scope.allowed_domains == []
    assert default_scope.allowed_urls == []
    assert default_scope.allowed_ports == []
    assert default_scope.local_only is False
    assert default_scope.max_concurrency == 1
    [host_policy] = await store.list_entities(
        AutomationProjectPolicy, engagement_id=scratch.id
    )
    assert host_policy.execution_mode == "host"
    assert host_policy.host_access_acknowledged is False
    assert (
        host_policy.metadata["host_access_source"] == "pending_operator_acknowledgement"
    )
    assert host_policy.network_enabled is False
    assert (await bootstrap_scratch_project(store)) == scratch.id
    assert (await store.count(Engagement)) == 1

    (await store.delete(Engagement, scratch.id, expected_revision=scratch.revision))

    assert (await bootstrap_scratch_project(store)) is None
    assert (await store.count(Engagement)) == 0


async def test_new_project_starts_with_durable_host_policy(tmp_path):
    store = NebulaStore(tmp_path / "projects.db")
    created = await create_engagement_with_default_scope(
        store, Engagement(name="Host project")
    )

    [policy] = await store.list_entities(
        AutomationProjectPolicy, engagement_id=created.id
    )
    assert policy.execution_mode == "host"
    assert policy.host_access_acknowledged is False
    assert policy.network_enabled is False
    assert policy.metadata["host_access_source"] == "pending_operator_acknowledgement"
    assert (await store.get(AutomationProjectPolicy, policy.id)) == policy


async def test_new_project_records_explicit_host_access_in_same_transaction(tmp_path):
    store = NebulaStore(tmp_path / "consented-project.db")
    created = await create_engagement_with_default_scope(
        store, Engagement(name="Consented Host project"), host_access_acknowledged=True
    )

    [policy] = await store.list_entities(
        AutomationProjectPolicy, engagement_id=created.id
    )
    assert policy.execution_mode == "host"
    assert policy.host_access_acknowledged is True
    assert policy.metadata["host_access_source"] == (
        "project_creation_operator_acknowledgement"
    )
    assert (await store.get(Engagement, created.id)) == created


async def test_project_create_request_is_atomic_retry_safe_and_retained_after_delete(
    tmp_path,
):
    store = NebulaStore(tmp_path / "project-retry.db")
    first = await create_engagement_with_default_scope(
        store,
        Engagement(name="Retry project"),
        actor_id="operator-a",
        request_id="request-one",
    )
    replay = await create_engagement_with_default_scope(
        store,
        Engagement(name="Retry project"),
        actor_id="operator-a",
        request_id="request-one",
    )
    assert replay == first
    assert (await store.count(Engagement)) == 1
    assert len((await store.list_entities(ScopePolicy, engagement_id=first.id))) == 1
    assert (
        len(
            (await store.list_entities(AutomationProjectPolicy, engagement_id=first.id))
        )
        == 1
    )
    async with store.database.session() as session:
        receipt = await session.get(
            ProjectCreateRequestRow, ("operator-a", "request-one")
        )
        assert receipt is not None and receipt.engagement_id == first.id

    with pytest.raises(ConflictError, match="different project details"):
        (
            await create_engagement_with_default_scope(
                store,
                Engagement(name="Changed details"),
                actor_id="operator-a",
                request_id="request-one",
            )
        )
    with pytest.raises(ConflictError, match="project name already exists"):
        (
            await create_engagement_with_default_scope(
                store,
                Engagement(name="  RETRY   project  "),
                actor_id="operator-a",
                request_id="request-two",
            )
        )
    other_actor = await create_engagement_with_default_scope(
        store,
        Engagement(name="Different project"),
        actor_id="operator-b",
        request_id="request-one",
    )
    assert other_actor.id != first.id

    (await store.delete(Engagement, first.id, expected_revision=first.revision))
    with pytest.raises(ConflictError, match="since been deleted"):
        (
            await create_engagement_with_default_scope(
                store,
                Engagement(name="Retry project"),
                actor_id="operator-a",
                request_id="request-one",
            )
        )


async def test_concurrent_project_create_retries_share_one_project(tmp_path):
    store = NebulaStore(tmp_path / "project-race.db")

    async def create(_index: int) -> Engagement:
        return await create_engagement_with_default_scope(
            store,
            Engagement(name="Concurrent project"),
            actor_id="operator-a",
            request_id="same-request",
        )

    projects = list((await asyncio.gather(*(create(_item) for _item in range(2)))))
    assert projects[0].id == projects[1].id
    assert (await store.count(Engagement)) == 1


async def test_concurrent_butler_creates_cannot_reuse_project_name(tmp_path):
    store = NebulaStore(tmp_path / "project-name-race.db")

    async def create(index: int) -> str:
        try:
            created = await create_engagement_with_default_scope(
                store,
                Engagement(
                    name="  Concurrent   project  " if index else "Concurrent project"
                ),
                actor_id="operator-a",
                request_id=f"request-{index}",
            )
            return created.id
        except ConflictError as exc:
            assert "project name already exists" in str(exc)
            return "conflict"

    results = list((await asyncio.gather(*(create(_item) for _item in range(2)))))
    assert results.count("conflict") == 1
    assert (await store.count(Engagement)) == 1


async def test_import_content_arriving_before_first_launch_suppresses_scratch(tmp_path):
    store = NebulaStore(tmp_path / "import.db")
    imported = await store.create(Engagement(name="Imported assessment"))

    assert (await bootstrap_scratch_project(store)) is None
    assert [item.id for item in (await store.list_entities(Engagement))] == [
        imported.id
    ]

    (await store.delete(Engagement, imported.id, expected_revision=imported.revision))
    assert (await bootstrap_scratch_project(store)) is None


async def test_existing_pre_bootstrap_database_is_not_treated_as_a_fresh_install(
    tmp_path,
):
    path = tmp_path / "upgrade.db"
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    _upgrade_to(engine, "0003_operation_events")
    engine.dispose()

    store = NebulaStore(Database(path))

    async with store.database.session() as session:
        marker = await session.get(BootstrapStateRow, "scratch_project_v1")
        assert marker is not None
        assert marker.status == "complete"
        assert marker.engagement_id is None
    assert (await bootstrap_scratch_project(store)) is None
    assert (await store.count(Engagement)) == 0


async def test_concurrent_scratch_bootstrap_is_idempotent(tmp_path):
    store = NebulaStore(tmp_path / "concurrent.db")
    results = list(
        (
            await asyncio.gather(
                *(
                    (lambda _: bootstrap_scratch_project(store))(_item)
                    for _item in range(8)
                )
            )
        )
    )

    assert results == ["scratch-project"] * 8
    assert (await store.count(Engagement)) == 1


async def test_concurrent_fresh_store_constructors_create_exactly_one_scratch(tmp_path):
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
from nebula.v3.async_utils import close_store_resources
import asyncio

database_path, gate_path, ready_path = sys.argv[1:]
Path(ready_path).touch()
gate = Path(gate_path)
while not gate.exists():
    time.sleep(0.005)
async def main():
    store = NebulaStore(database_path)
    try:
        print(await bootstrap_scratch_project(store), flush=True)
    finally:
        await close_store_resources([store])
asyncio.run(main())
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
        assert [item.id for item in (await store.list_entities(Engagement))] == [
            "scratch-project"
        ]
        async with store.database.session() as session:
            markers = session.query(BootstrapStateRow).all()
        assert len(markers) == 1
        assert markers[0].key == "scratch_project_v1"
        assert markers[0].status == "complete"
        assert markers[0].engagement_id == "scratch-project"
    finally:
        (await store.database.dispose())


async def test_interrupted_fresh_migration_preserves_scratch_eligibility(
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
        assert (await bootstrap_scratch_project(store)) == "scratch-project"
        assert (await store.count(Engagement)) == 1
    finally:
        (await store.database.dispose())


async def test_setup_status_contract_is_authenticated_and_model_optional(tmp_path):
    store = NebulaStore(tmp_path / "api.db")
    app = create_app(store, auth_token=TOKEN, bootstrap_workspace=True)
    client = TestClient(app)

    assert (await client.get("/api/v1/setup/status")).status_code == 401
    response = await client.get("/api/v1/setup/status", headers=AUTH)

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


async def test_create_app_does_not_bootstrap_embedded_stores_unless_requested(tmp_path):
    store = NebulaStore(tmp_path / "embedded.db")

    create_app(store, auth_token=TOKEN)

    assert (await store.count(Engagement)) == 0


async def test_prepared_runtime_survives_health_refresh_but_not_runner_config_change(
    tmp_path,
):
    store = NebulaStore(tmp_path / "runner-fingerprint.db")
    engagement = await store.create(Engagement(name="Runner fingerprint"))
    profile = await store.create(
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
    refreshed = await store.update(
        RunnerProfile,
        profile.id,
        {"last_health_at": "2026-07-18T12:00:00Z", "last_health_detail": "ready"},
        expected_revision=profile.revision,
    )

    assert (
        await platform.resolve_operator_runtime(engagement.id, "bash", network=False)
    ).profile.revision == refreshed.revision

    changed = await store.update(
        RunnerProfile,
        refreshed.id,
        {"context": "alternate-rootless-context"},
        expected_revision=refreshed.revision,
    )
    with pytest.raises(RuntimePlatformError, match="runner has changed"):
        (await platform.resolve_operator_runtime(engagement.id, "bash", network=False))
    assert changed.revision > profile.revision


async def test_human_terminal_runner_admits_only_the_exact_linked_workspace(tmp_path):
    store = NebulaStore(tmp_path / "linked-workspace.db")
    linked_workspace = tmp_path / "linked" / "project"
    linked_workspace.mkdir(parents=True)
    sibling_workspace = tmp_path / "linked" / "other-project"
    sibling_workspace.mkdir()
    engagement = await store.create(
        Engagement(name="Linked workspace", workspace_path=str(linked_workspace))
    )
    profile = await store.create(
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
    image = PreparedContainerImage(
        source_reference="docker.io/kalilinux/kali-rolling:latest",
        base_resolved_reference=("docker.io/kalilinux/kali-rolling@sha256:" + "b" * 64),
        base_digest="sha256:" + "b" * 64,
        resolved_reference="sha256:" + "c" * 64,
        digest="sha256:" + "c" * 64,
        platform="linux/amd64",
        configured_user="",
        installed_packages=("kali-linux-headless",),
        refreshed=False,
        detail="verified cached image",
    )
    platform._prepared_images[(profile.id, profile.revision)] = image

    resolution = await platform.resolve_human_terminal_runtime(engagement.id)

    assert resolution.workspace == linked_workspace.resolve()
    assert resolution.runner.egress_controller.certified is True
    assert linked_workspace.resolve() in resolution.runner.workspace_roots
    assert (tmp_path / "linked").resolve() not in resolution.runner.workspace_roots
    allowed = SandboxRequest(
        image=image.digest,
        trusted_local_image=True,
        command=["/bin/true"],
        workspace=linked_workspace,
        workspace_access=SandboxWorkspaceAccess.WRITE,
    )
    assert resolution.runner._validate(allowed) == linked_workspace.resolve()
    denied = allowed.model_copy(update={"workspace": sibling_workspace})
    with pytest.raises(SandboxError, match="outside the configured workspace roots"):
        resolution.runner._validate(denied)

    platform.runtime_metadata_path.write_text(
        json.dumps(
            {
                "schema": KALI_RUNTIME_METADATA_SCHEMA,
                "image_digest": image.digest,
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
    operator = await platform.resolve_operator_runtime(
        engagement.id, "bash", network=False
    )
    assert operator.workspace == linked_workspace.resolve()
    assert linked_workspace.resolve() in operator.runner.workspace_roots
    assert (tmp_path / "linked").resolve() not in operator.runner.workspace_roots


async def test_exactly_one_verified_fixed_runtime_is_persisted_as_local(
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

    status = await service.refresh()

    assert status.terminal.status == "needs_image"
    assert status.terminal.image_preparation.phase == "not_started"
    assert status.terminal.runner_profile_id == "local"
    assert [candidate.executable for candidate in status.terminal.candidates] == [
        str(runtime)
    ]
    profile = await store.get(RunnerProfile, "local")
    assert profile.executable == str(runtime)
    assert profile.healthy is True


async def test_multiple_healthy_fixed_runtimes_require_selection_and_persist_nothing(
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

    status = await service.refresh()

    assert status.terminal.status == "needs_runner"
    assert {candidate.runtime.value for candidate in status.terminal.candidates} == {
        "docker",
        "podman",
    }
    assert (await store.count(RunnerProfile)) == 0


async def test_runtime_detection_never_searches_path_or_environment_override(
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

    status = await service.refresh()

    assert status.terminal.status == "needs_runner"
    assert status.terminal.candidates == []
    assert (await store.count(RunnerProfile)) == 0


async def test_fixed_candidate_selection_is_idempotent_and_never_accepts_a_path(
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

    candidate, selected, repeated = await scenario()

    assert selected.accepted is True
    assert selected.idempotent is False
    assert selected.setup.terminal.status == "needs_image"
    assert repeated.setup.terminal.status == "needs_image"
    assert selected.setup.terminal.runner_profile_id == "local"
    assert repeated.idempotent is True
    profile = await store.get(RunnerProfile, "local")
    assert profile.executable == candidate.executable == str(runtimes[0])
    assert profile.context == candidate.context


async def test_setup_event_replay_is_monotonic_snapshot_bounded_and_reports_gaps(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", ())
    store = NebulaStore(tmp_path / "events.db")
    service = SetupService(store, _platform(tmp_path, store), event_retention=3)

    async def refresh_repeatedly():
        await service.refresh()
        await service.refresh()
        await service.refresh()

    (await refresh_repeatedly())
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


async def test_image_preparation_reports_phases_can_cancel_and_retry(
    tmp_path, monkeypatch
):
    store = NebulaStore(tmp_path / "preparation.db")
    project = await store.create(Engagement(name="Image preparation"))
    (
        await store.create(
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
    ) = await scenario()

    assert first.operation_id != retry.operation_id != final_retry.operation_id
    assert duplicate.idempotent is True
    assert duplicate.operation_id == retry.operation_id
    assert cancelled.accepted is True
    assert cancelled.setup.terminal.image_preparation.phase == "cancelled"
    assert cancelled.setup.terminal.status == "needs_image"
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


async def test_setup_control_api_and_sse_are_authenticated_and_path_closed(
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

    async with TestClient(app) as client:
        assert (
            await client.get("/api/v1/setup/events?follow=false")
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/setup/runtime/select",
                json={"candidate_id": "fixed:" + "a" * 32},
            )
        ).status_code == 401

        refreshed = (
            await client.post("/api/v1/setup/runtime/refresh", headers=AUTH)
        ).json()
        candidate_id = refreshed["terminal"]["candidates"][0]["candidate_id"]
        rejected_path = await client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id, "executable": "/tmp/evil"},
        )
        assert rejected_path.status_code == 422

        selected = await client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id},
        )
        assert selected.status_code == 200
        assert selected.json()["setup"]["terminal"]["runner_profile_id"] == "local"
        repeated = await client.post(
            "/api/v1/setup/runtime/select",
            headers=AUTH,
            json={"candidate_id": candidate_id},
        )
        assert repeated.json()["idempotent"] is True

        prepared = await client.post(
            "/api/v1/setup/image/prepare",
            headers=AUTH,
            json={"project_id": "scratch-project"},
        )
        assert prepared.status_code == 200
        operation_id = prepared.json()["operation_id"]
        for _ in range(100):
            status = (await client.get("/api/v1/setup/status", headers=AUTH)).json()
            if status["terminal"]["image_preparation"]["phase"] == "ready":
                break
        else:
            pytest.fail("image preparation API did not reach ready")

        retried = await client.post(
            "/api/v1/setup/image/retry",
            headers=AUTH,
            json={"project_id": "scratch-project"},
        )
        assert retried.json()["idempotent"] is True
        cancelled_ready = await client.post(
            "/api/v1/setup/image/cancel",
            headers=AUTH,
            json={"operation_id": operation_id},
        )
        assert cancelled_ready.json()["idempotent"] is True

        events = await client.get("/api/v1/setup/events?follow=false", headers=AUTH)
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

        resumed = await client.get(
            "/api/v1/setup/events?follow=false",
            headers={**AUTH, "Last-Event-ID": str(ids[-2])},
        )
        resumed_ids = [
            int(line.removeprefix("id: "))
            for line in resumed.text.splitlines()
            if line.startswith("id: ")
        ]
        assert resumed_ids == [ids[-1]]


async def test_idle_image_preparation_reports_needs_image_not_preparing(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "trusted" / "podman"
    runtime.parent.mkdir()
    runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runtime.chmod(0o755)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", (runtime,))

    async def healthy(_runner):
        return True, "verified local runtime"

    monkeypatch.setattr(ContainerSandboxRunner, "available", healthy)
    store = NebulaStore(tmp_path / "idle-image.db")
    project = await store.create(Engagement(name="Idle image"))
    platform = _platform(tmp_path, store)
    service = SetupService(store, platform)
    started = asyncio.Event()
    release = asyncio.Event()

    async def resolve(project_id, *, on_progress=None):
        del project_id, on_progress
        started.set()
        await release.wait()
        return SimpleNamespace(
            image=SimpleNamespace(
                digest="sha256:" + "a" * 64,
                detail="verified cached workstation image",
            )
        )

    monkeypatch.setattr(platform, "resolve_human_terminal_runtime", resolve)

    async def scenario():
        detected = await service.refresh()
        control = await service.prepare_image(
            ImagePreparationRequest(project_id=project.id)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        preparing = await service.status()
        cancelled = await service.cancel_image_preparation(
            ImagePreparationCancellationRequest(operation_id=control.operation_id)
        )
        release.set()
        await service.retry_image_preparation(
            ImagePreparationRequest(project_id=project.id)
        )
        for _ in range(100):
            ready = await service.status()
            if ready.terminal.image_preparation.phase == "ready":
                break
            await asyncio.sleep(0)
        return detected, preparing, cancelled, ready

    detected, preparing, cancelled, ready = await scenario()

    # A selected runner with nothing running is idle, not "preparing_image":
    # Settings would otherwise show "Preparing Kali runtime…" and poll forever.
    assert detected.terminal.runner_profile_id == "local"
    assert detected.terminal.image_preparation.phase == "not_started"
    assert detected.terminal.status == "needs_image"
    assert detected.application_stage == "ready"
    assert preparing.terminal.image_preparation.phase == "preparing_image"
    assert preparing.terminal.status == "preparing_image"
    # Cancel leaves nothing running either.
    assert cancelled.setup.terminal.image_preparation.phase == "cancelled"
    assert cancelled.setup.terminal.status == "needs_image"
    assert cancelled.setup.terminal.image_preparation.can_retry is True
    assert ready.terminal.image_preparation.phase == "ready"
    assert ready.terminal.status == "ready"


pytestmark = pytest.mark.anyio
