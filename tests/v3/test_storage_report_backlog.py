"""A storage source token cannot build a backlog that costs the operator anything."""

import asyncio
import json

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select, update

from nebula.v3.database import RuntimeCommandRow, RuntimeDeliveryRow
from nebula.v3.domain import Engagement, ProviderProfile, StorageSource
from nebula.v3.runtime_coordination import (
    REPORT_LANE,
    STORAGE_REPORT_CANCELLED,
    STORAGE_REPORT_COMMAND,
)
from nebula.v3.runtime_delivery import DurableHttpRunner
from nebula.v3.storage_integrations import (
    StorageIntegrationService,
    StorageSourcePatch,
    storage_router,
)
from nebula.v3.storage_report_admission import StorageReportControl
from tests.v3.test_api_worker import (  # noqa: F401
    _AUTH,
    _TOKEN,
    _accepted,
    _storage_report,
    _storage_source,
    anyio_backend,
)
from tests.v3.test_api_worker import boundary as boundary  # noqa: F401
from tests.v3.test_runtime_coordination import location as location  # noqa: F401

pytestmark = pytest.mark.anyio


async def _reports(boundary, **where):
    async with boundary.host.database.session() as session:
        return list(
            await session.scalars(
                select(RuntimeCommandRow)
                .where(
                    RuntimeCommandRow.name == STORAGE_REPORT_COMMAND,
                    *(
                        getattr(RuntimeCommandRow, key) == value
                        for key, value in where.items()
                    ),
                )
                .order_by(RuntimeCommandRow.sequence)
            )
        )


def _vault_files(boundary):
    return {
        path.name
        for path in boundary.delivery.vault.root.iterdir()
        if not path.name.startswith(".") and path.name != "key"
    }


async def test_a_source_and_its_project_hold_a_bounded_backlog(boundary, monkeypatch):
    import nebula.v3.storage_report_admission as admission

    monkeypatch.setattr(admission, "STORAGE_REPORT_SOURCE_LIMIT", 3)
    monkeypatch.setattr(admission, "STORAGE_REPORT_PROJECT_LIMIT", 5)
    first_source, first = await _storage_source(boundary)
    _, second = await _storage_source(boundary, secret=b"second-secret")
    _, client = boundary.api()
    # Concurrent posts cannot overshoot: the bound is enforced where it saves.
    bodies = [_storage_report(f"flood-{index}") for index in range(10)]
    answers = await asyncio.gather(
        *(
            client.post("/api/v1/storage/events", headers=first, json=body)
            for body in bodies
        )
    )
    statuses = sorted(answer.status_code for answer in answers)
    assert statuses == [202] * 3 + [429] * 7, [a.text for a in answers]
    refused = next(answer for answer in answers if answer.status_code == 429)
    assert refused.headers["retry-after"] == "15"
    assert "retry the same report" in refused.json()["detail"]
    assert len(await _reports(boundary)) == 3
    files = _vault_files(boundary)
    # A resend of a report that is already saved is never refused.
    index, saved = next(
        (index, answer)
        for index, answer in enumerate(answers)
        if answer.status_code == 202
    )
    resent = await client.post(
        "/api/v1/storage/events", headers=first, json=bodies[index]
    )
    assert resent.status_code == 202
    assert resent.json()["command_id"] == saved.json()["command_id"]
    async with boundary.host.database.session() as session:
        row = await session.get(RuntimeCommandRow, saved.json()["command_id"])
    # Another source still has room until the project's ceiling.
    for index in range(2):
        assert (
            await client.post(
                "/api/v1/storage/events",
                headers=second,
                json=_storage_report(f"second-{index}"),
            )
        ).status_code == 202
    over = await client.post(
        "/api/v1/storage/inventory",
        headers=second,
        json={"schema_version": 1, "devices": [{"id": "nas", "label": "NAS"}]},
    )
    assert over.status_code == 429 and "project" in over.json()["detail"]
    assert len(await _reports(boundary)) == 5
    # Refusals left nothing in the vault.
    assert len(_vault_files(boundary)) == len(files) + 2
    # Once a report runs, the source has room again.
    runner = DurableHttpRunner(
        boundary.delivery, await _accepted(), operator_token=_TOKEN
    )
    assert await runner.run_one(boundary.lease, lane=REPORT_LANE)
    again = await client.post(
        "/api/v1/storage/events", headers=first, json=_storage_report("after-run")
    )
    assert again.status_code == 202, again.text
    assert row.actor_id.startswith(f"storage-source:{first_source.id}:")


async def test_a_storage_backlog_never_delays_the_operators_work(boundary):
    _, headers = await _storage_source(boundary)
    _, client = boundary.api()
    for index in range(30):
        saved = await client.post(
            "/api/v1/storage/events",
            headers=headers,
            json=_storage_report(f"backlog-{index}"),
        )
        assert saved.status_code == 202, saved.text
    operator = await client.post(
        "/api/v1/synthetic", headers=_AUTH, json={"text": "the operator's turn"}
    )
    assert operator.status_code == 202
    # Reports have their own ordered lane; the operator's command is the next
    # work-lane claim however many reports wait.
    assert {row.lane for row in await _reports(boundary)} == {REPORT_LANE}
    claimed = await boundary.coordination.claim_next(boundary.lease, lane="work")
    assert claimed.receipt.command_id == operator.json()["receipt"]["command_id"]
    # ...and both lanes run at once: a report claims while that command runs.
    report = await boundary.coordination.claim_next(boundary.lease, lane=REPORT_LANE)
    assert report is not None and report.name == STORAGE_REPORT_COMMAND


async def test_padded_copies_of_one_event_are_one_report(boundary):
    _, headers = await _storage_source(boundary)
    _, client = boundary.api()
    report = _storage_report()
    report["observed_at"] = "2026-11-14T10:00:00+00:00"
    compact = json.dumps(report, separators=(",", ":")).encode()
    padded = b"  \n" + json.dumps(report, indent=4, sort_keys=True).encode() + b"\n\n  "
    zulu = json.dumps(
        {**report, "observed_at": "2026-11-14T10:00:00Z"}, sort_keys=False
    ).encode()
    answers = []
    for body in (compact, padded, zulu):
        answer = await client.post(
            "/api/v1/storage/events",
            headers={**headers, "Content-Type": "application/json"},
            content=body,
        )
        assert answer.status_code == 202, answer.text
        answers.append(answer.json()["command_id"])
    assert len(set(answers)) == 1
    assert len(await _reports(boundary)) == 1


@pytest.mark.parametrize(
    "path",
    ["/api/v1/storage/events", "/api/v1/storage/inventory", "/api/v1/storage/test"],
)
async def test_a_body_that_is_not_utf8_is_refused_as_invalid(boundary, path):
    _, headers = await _storage_source(boundary)
    _, client = boundary.api()
    for body in (b"\xff\xfe", json.dumps(_storage_report()).encode("utf-16")):
        refused = await client.post(
            path,
            headers={**headers, "Content-Type": "application/json"},
            content=body,
        )
        assert refused.status_code == 422, refused.text
        # The sender's bytes are never echoed back.
        assert "input" not in json.dumps(refused.json())
    assert await _reports(boundary) == []


def _service(boundary):
    service = StorageIntegrationService(boundary.host, None, None, None)
    service.bind_report_control(
        StorageReportControl(boundary.coordination, boundary.lease)
    )
    return service


async def test_disabling_revoking_rotating_or_moving_a_source_cancels_its_queued_reports(
    boundary,
):
    await boundary.host.create(
        ProviderProfile(
            id="provider",
            name="Local model",
            provider_type="openai_compatible",
            model_allowlist=["model"],
        )
    )
    await boundary.host.create(Engagement(id="other", name="Synthetic other"))
    source, headers = await _storage_source(boundary)
    _, unrelated = await _storage_source(boundary, secret=b"unrelated-secret")
    service = _service(boundary)
    app = FastAPI()
    app.include_router(storage_router(service, lambda: "operator"))
    operator = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://core.test"
    )
    _, client = boundary.api()
    seen = []

    async def execution(scope, receive, send):
        seen.append(scope["path"])
        await (await _accepted())(scope, receive, send)

    runner = DurableHttpRunner(boundary.delivery, execution, operator_token=_TOKEN)

    async def save(prefix, sender=None, count=3):
        sender = sender or headers
        ids = []
        for index in range(count):
            answer = await client.post(
                "/api/v1/storage/events",
                headers=sender,
                json=_storage_report(f"{prefix}-{index}"),
            )
            assert answer.status_code == 202, answer.text
            ids.append(answer.json()["command_id"])
        return ids

    async def states(ids):
        async with boundary.host.database.session() as session:
            return [
                ((row := await session.get(RuntimeCommandRow, item)).state, row.result)
                for item in ids
            ]

    kept = await save("unrelated", unrelated, 1)
    cancelled = [(("failed", STORAGE_REPORT_CANCELLED))] * 3

    # Disabled: every queued report of that source is refused without running.
    queued = await save("disabled")
    current = await boundary.host.get(StorageSource, source.id)
    await service.update_source(
        source.id,
        StorageSourcePatch(enabled=False, expected_revision=current.revision),
    )
    assert await states(queued) == cancelled
    current = await boundary.host.get(StorageSource, source.id)
    await service.update_source(
        source.id, StorageSourcePatch(enabled=True, expected_revision=current.revision)
    )

    # Rotated: reports carrying the old token are refused.
    queued = await save("rotated")
    rotated = await operator.post(f"/api/v1/storage/sources/{source.id}/rotate")
    assert rotated.status_code == 200, rotated.text
    assert await states(queued) == cancelled
    headers = {"Authorization": "Bearer " + rotated.json()["token"]}

    # Moved to another project: its queued reports here can never run.
    queued = await save("moved")
    current = await boundary.host.get(StorageSource, source.id)
    await service.update_source(
        source.id,
        StorageSourcePatch(engagement_id="other", expected_revision=current.revision),
    )
    assert await states(queued) == cancelled
    current = await boundary.host.get(StorageSource, source.id)
    await service.update_source(
        source.id,
        StorageSourcePatch(engagement_id="project", expected_revision=current.revision),
    )

    # Revoked.
    queued = await save("revoked")
    revoked = await operator.post(f"/api/v1/storage/sources/{source.id}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert await states(queued) == cancelled

    # Nothing ran for the cancelled reports; another source's report still runs.
    assert await states(kept) == [("queued", None)]
    assert await runner.run_one(boundary.lease, lane=REPORT_LANE)
    assert not await runner.run_one(boundary.lease, lane=REPORT_LANE)
    assert seen == ["/api/v1/storage/events"]
    await operator.aclose()


async def test_a_report_found_invalid_at_run_takes_its_principals_queue_with_it(
    boundary,
):
    source, headers = await _storage_source(boundary)
    _, client = boundary.api()
    ids = []
    for index in range(4):
        answer = await client.post(
            "/api/v1/storage/events",
            headers=headers,
            json=_storage_report(f"stale-{index}"),
        )
        ids.append(answer.json()["command_id"])
    # Disabled where no report control was bound (an older worker, a race).
    current = await boundary.host.get(StorageSource, source.id)
    await boundary.host.update(
        StorageSource,
        source.id,
        {"enabled": False},
        expected_revision=current.revision,
    )
    runner = DurableHttpRunner(
        boundary.delivery, await _accepted(), operator_token=_TOKEN
    )
    assert await runner.run_one(boundary.lease, lane=REPORT_LANE)
    # One run refused the first; the rest were settled with it, not one by one.
    assert not await runner.run_one(boundary.lease, lane=REPORT_LANE)
    rows = {row.id: row for row in await _reports(boundary)}
    assert rows[ids[0]].result == {"http_status": 401}
    assert [rows[item].result for item in ids[1:]] == [STORAGE_REPORT_CANCELLED] * 3


async def test_finished_reports_and_their_payloads_are_pruned(boundary, monkeypatch):
    import nebula.v3.runtime_delivery as runtime_delivery

    _, headers = await _storage_source(boundary)
    _, client = boundary.api()
    ok = DurableHttpRunner(boundary.delivery, await _accepted(), operator_token=_TOKEN)
    broken = DurableHttpRunner(
        boundary.delivery,
        await _accepted(500, b'{"detail":"boom"}'),
        operator_token=_TOKEN,
    )
    report = _storage_report("retained")
    first = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert await broken.run_one(boundary.lease, lane=REPORT_LANE)
    # The retry chains a new run of the same body; both share one payload.
    second = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert second.json()["command_id"] != first.json()["command_id"]
    assert await ok.run_one(boundary.lease, lane=REPORT_LANE)
    other = await client.post(
        "/api/v1/storage/events", headers=headers, json=_storage_report("other")
    )
    assert await ok.run_one(boundary.lease, lane=REPORT_LANE)
    rows = await _reports(boundary)
    payloads = {row.id: row.payload["request_reference"] for row in rows}
    assert payloads[first.json()["command_id"]] == payloads[second.json()["command_id"]]
    assert {payloads[row.id] for row in rows} <= _vault_files(boundary)

    # Age only the failed run past retention: its frames go, the shared
    # payload stays for the run that still exists.
    async with boundary.host.database.session() as session:
        await session.execute(
            update(RuntimeCommandRow)
            .where(RuntimeCommandRow.id == first.json()["command_id"])
            .values(accepted_at_ms=0)
        )
        await session.commit()
    assert await boundary.delivery.prune_storage_reports(boundary.lease) == 1
    assert payloads[first.json()["command_id"]] in _vault_files(boundary)
    # Beyond the retained count, the oldest finished reports go too.
    monkeypatch.setattr(runtime_delivery, "STORAGE_REPORT_RETAINED", 1)
    monkeypatch.setattr(runtime_delivery, "STORAGE_REPORT_MINIMUM_AGE_MS", -60_000)
    assert await boundary.delivery.prune_storage_reports(boundary.lease) == 1
    remaining = await _reports(boundary)
    assert [row.id for row in remaining] == [other.json()["command_id"]]
    async with boundary.host.database.session() as session:
        frames = list(await session.scalars(select(RuntimeDeliveryRow)))
    assert {frame.command_id for frame in frames} == {other.json()["command_id"]}
    assert _vault_files(boundary) == {payloads[other.json()["command_id"]]} | {
        frame.content_reference for frame in frames
    }
    # A resend after pruning is a new run; Core dedupes it by event ID.
    resent = await client.post("/api/v1/storage/events", headers=headers, json=report)
    assert resent.status_code == 202
    assert resent.json()["command_id"] not in payloads
    assert await ok.run_one(boundary.lease, lane=REPORT_LANE)


async def test_the_reference_sender_waits_for_retry_after(monkeypatch):
    from urllib.error import HTTPError

    from tests.v3.test_storage_sender import sender_module

    sender = sender_module()
    slept, attempts = [], []

    class Accepted:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return b'{"accepted":true}'

    def post(request, timeout):
        attempts.append(request.data)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "15"}, None)
        return Accepted()

    monkeypatch.setattr(sender, "urlopen", post)
    monkeypatch.setattr(sender.time, "sleep", slept.append)
    monkeypatch.setattr(sender.random, "random", lambda: 0.0)
    assert sender.send("https://core.test/api/v1/storage/events", "t", b"{}") == {
        "accepted": True
    }
    assert slept == [15.0] and len(attempts) == 2
    assert sender.retry_after("600") == 60.0 and sender.retry_after("soon") is None
