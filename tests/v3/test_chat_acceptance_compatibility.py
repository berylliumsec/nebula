"""Feature negotiation protects retained APIs and already accepted chat work."""

from pathlib import Path

import pytest

from nebula.v3.runtime_coordination import CoordinationConflict
from nebula.v3.runtime_delivery import HttpIntent, RuntimeDelivery
from nebula.v3.runtime_vault import RuntimeVault
from tests.v3.test_deployment_lifecycle import host as host, manifest, worker
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio
FEATURE = "chat.accept/v1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def stage_feature(host):
    await host[0].stage(
        manifest("feature", features=[FEATURE]),
        Path("/isolated/releases/feature"),
        "b" * 64,
    )


async def test_resume_receipts_and_resident_workers_require_the_resume_reader(
    host, tmp_path
):
    coordinator, _ = host
    await stage_feature(host)
    old_api = await worker(host, "feature", "api")
    await coordinator.stage(
        manifest("resume", features=[FEATURE, "chat.resume/v1"]),
        Path("/isolated/releases/resume"),
        "c" * 64,
    )
    new_api = await worker(host, "resume", "api")
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(tmp_path / "payloads")
    )
    await delivery.accept(
        HttpIntent.capture("POST", "/api/v1/chat/turns/turn/resume", b"", [], b""),
        project_id="one",
        actor_id="operator",
        idempotency_key="resume",
        chat_resume=True,
    )
    with pytest.raises(CoordinationConflict, match="chat.resume/v1"):
        await coordinator.promote_api(
            old_api, expected_revision=(await coordinator.status()).revision
        )
    await coordinator.promote_api(
        new_api, expected_revision=(await coordinator.status()).revision
    )
    assert await worker(host, "resume") is not None


async def test_queued_resume_cannot_transfer_to_an_older_execution_worker(
    host, tmp_path
):
    coordinator, _ = host
    await coordinator.stage(
        manifest("resume", features=[FEATURE, "chat.resume/v1"]),
        Path("/isolated/releases/resume"),
        "c" * 64,
    )
    source, target = await worker(host, "resume"), await worker(host, "v1")
    lease = await coordinator.coordination.claim_project("one", source)
    await coordinator.request_handoff("one", target)
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(tmp_path / "payloads")
    )
    await delivery.accept(
        HttpIntent.capture("POST", "/api/v1/chat/turns/turn/resume", b"", [], b""),
        project_id="one",
        actor_id="operator",
        idempotency_key="resume",
        chat_resume=True,
    )
    await coordinator.checkpoint(lease, checkpoint={}, blockers=[])
    with pytest.raises(CoordinationConflict, match="accepted chat requests"):
        await coordinator.transfer("one")
    async with coordinator.coordination.fenced(lease):
        pass


async def test_resident_feature_worker_blocks_an_older_api_promotion(host):
    coordinator, _ = host
    old_api = await worker(host, "v1", "api")
    await stage_feature(host)
    execution = await worker(host, "feature")
    with pytest.raises(CoordinationConflict, match="required by resident"):
        await coordinator.promote_api(
            old_api, expected_revision=(await coordinator.status()).revision
        )
    assert (await coordinator.status()).api_version is None
    new_api = await worker(host, "feature", "api")
    await coordinator.promote_api(
        new_api, expected_revision=(await coordinator.status()).revision
    )
    result = await coordinator.status()
    assert result.api_version == "feature" and result.resident_versions == ["feature"]
    assert execution is not None


async def test_feature_execution_waits_for_a_compatible_active_api(host):
    coordinator, _ = host
    old_api = await worker(host, "v1", "api")
    await coordinator.promote_api(
        old_api, expected_revision=(await coordinator.status()).revision
    )
    await stage_feature(host)
    with pytest.raises(CoordinationConflict, match="Promote an API"):
        await worker(host, "feature")
    new_api = await worker(host, "feature", "api")
    await coordinator.promote_api(
        new_api, expected_revision=(await coordinator.status()).revision
    )
    assert await worker(host, "feature") is not None


async def test_accepted_typed_receipts_protect_api_rollback_even_without_execution(
    host, tmp_path
):
    coordinator, _ = host
    old_api = await worker(host, "v1", "api")
    delivery = RuntimeDelivery(
        coordinator.coordination, RuntimeVault(tmp_path / "payloads")
    )
    intent = HttpIntent.capture(
        "POST",
        "/api/v1/chat/completions",
        b"",
        [(b"content-type", b"application/json")],
        b'{"stream":true}',
    )
    receipt = await delivery.accept(
        intent,
        project_id="one",
        actor_id="operator",
        idempotency_key="accepted-feature",
        chat_acceptance=True,
    )
    assert receipt.project_id == "one"
    with pytest.raises(CoordinationConflict, match="accepted work"):
        await coordinator.promote_api(
            old_api, expected_revision=(await coordinator.status()).revision
        )
    await stage_feature(host)
    new_api = await worker(host, "feature", "api")
    await coordinator.promote_api(
        new_api, expected_revision=(await coordinator.status()).revision
    )
    assert (await coordinator.status()).api_version == "feature"


def test_feature_lists_are_bounded_stable_and_optional_for_old_manifests():
    from pydantic import ValidationError

    assert manifest("old").features == []
    assert manifest(
        "new", features=[FEATURE, FEATURE], required_api_features=[FEATURE, FEATURE]
    ).features == [FEATURE]
    with pytest.raises(ValidationError):
        manifest("invalid", features=["Unversioned Invalid Feature"])
